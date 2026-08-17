"""Simple Streamlit UI — two actions: sell ≥30% profits, research & buy 2–3 stocks."""

from __future__ import annotations

import os

from dataclasses import replace

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from trading_bot.config import ROOT, load_settings
from trading_bot.execution import list_open_positions
from trading_bot.kite_auth import (
    clear_session,
    exchange_request_token,
    kite_redirect_url,
    login_url,
    profile_status,
    upsert_env,
)
from trading_bot.runtime import boot, build_provider
from trading_bot.simple_bot import (
    DEFAULT_PRICE_BANDS,
    PRICE_BANDS,
    PROFIT_SELL_PCT,
    auto_sell_profits,
    fetch_kite_holdings,
    fetch_kite_quote,
    manual_buy,
    manual_sell_holdings,
    place_selected_buys,
    plan_buys_from_capital,
    research_and_buy,
)
from trading_bot.universe import (
    DEFAULT_INDEX_FILTERS,
    INDEX_LABELS,
    refresh_universe,
)

try:
    st.set_page_config(page_title="NSE Simple Bot", layout="wide")
except st.errors.StreamlitAPIException:
    pass


def _restore_kite_token_from_session() -> None:
    token = st.session_state.get("kite_access_token")
    if token:
        os.environ["KITE_ACCESS_TOKEN"] = str(token)


def _boot():
    load_dotenv(ROOT / ".env", override=False)
    _restore_kite_token_from_session()
    settings, conn = boot()
    provider = build_provider(settings, conn)
    return settings, conn, provider


def _consume_kite_callback(settings) -> None:
    params = st.query_params
    token = params.get("request_token")
    status = params.get("status")
    if isinstance(token, list):
        token = token[0] if token else None
    if isinstance(status, list):
        status = status[0] if status else None
    if not token:
        return
    if status and status != "success":
        st.error(f"Kite login failed (status={status}).")
        st.query_params.clear()
        return
    try:
        session = exchange_request_token(str(token), settings)
        access = session.get("access_token")
        if access:
            st.session_state["kite_access_token"] = access
            os.environ["KITE_ACCESS_TOKEN"] = str(access)
            st.success("Kite connected.")
        st.query_params.clear()
    except Exception as exc:
        st.error(f"Could not exchange Kite token: {exc}")
        st.query_params.clear()


def _setup_sidebar(settings) -> None:
    with st.sidebar:
        st.markdown("### Setup")
        st.caption("Same secrets as before (.env / Streamlit Secrets).")
        live = st.toggle(
            "Send real orders to Zerodha",
            value=not settings.paper_mode,
            key="live_orders",
            help="Off = paper simulation (app book only). On = live CNC market orders on Kite.",
        )
        if live:
            st.error("LIVE — buys/sells go to your Kite account")
        else:
            st.warning("PAPER — nothing is sent to Zerodha. This is why ‘orders placed’ did not hit your account.")

        kite = profile_status(settings)
        if kite.get("ok"):
            st.success(f"Kite: {kite.get('user_id') or 'connected'}")
            if st.button("Disconnect Kite"):
                clear_session()
                st.session_state.pop("kite_access_token", None)
                os.environ.pop("KITE_ACCESS_TOKEN", None)
                st.rerun()
        else:
            st.warning(f"Kite: {kite.get('reason') or 'not connected'}")
            if settings.kite_api_key and settings.kite_api_secret:
                st.link_button("Connect Kite", login_url(settings.kite_api_key), type="primary")
                st.caption(f"Redirect: `{kite_redirect_url(settings)}`")
            else:
                st.error("Add KITE_API_KEY + KITE_API_SECRET in Secrets / .env")

        st.caption(
            "Finnhub: "
            + ("ready" if settings.finnhub_ready else "missing FINNHUB_API_KEY (news skipped)")
        )
        with st.expander("Save keys locally (optional)"):
            api_key = st.text_input("KITE_API_KEY", value=settings.kite_api_key or "", type="password")
            api_secret = st.text_input(
                "KITE_API_SECRET", value=settings.kite_api_secret or "", type="password"
            )
            finnhub_key = st.text_input(
                "FINNHUB_API_KEY", value=settings.finnhub_api_key or "", type="password"
            )
            if st.button("Save to .env"):
                updates = {}
                if api_key.strip():
                    updates["KITE_API_KEY"] = api_key.strip()
                if api_secret.strip():
                    updates["KITE_API_SECRET"] = api_secret.strip()
                if finnhub_key.strip():
                    updates["FINNHUB_API_KEY"] = finnhub_key.strip()
                if updates:
                    upsert_env(updates)
                    st.success("Saved — on Cloud put keys in Streamlit Secrets instead.")
                else:
                    st.info("Nothing to save.")

        st.markdown("---")
        st.caption("Secrets: Kite + Finnhub. No OpenAI required.")


def main() -> None:
    settings, conn, provider = _boot()
    _consume_kite_callback(load_settings())
    settings, conn, provider = _boot()
    _setup_sidebar(settings)
    live = bool(st.session_state.get("live_orders"))
    settings = replace(settings, paper_mode=not live)

    st.title("NSE Simple Bot")
    st.markdown(
        """
1. **Auto-sell** ≥30% · **Manual sell** · **Manual buy**  
2. **Research** — suggest → **you pick** which names to buy (qty from your capital)  
   (rules + Finnhub + Kite; no ChatGPT)
"""
    )

    kite = profile_status(settings)
    c1, c2, c3 = st.columns(3)
    c1.metric("Paper mode", "ON" if settings.paper_mode else "OFF")
    c2.metric("Kite", "connected" if kite.get("ok") else "not connected")
    c3.metric("Finnhub", "ready" if settings.finnhub_ready else "add key")

    st.markdown("---")
    st.subheader("1 · Auto-sell ≥30% profits")
    st.caption("Pulls live holdings from your Kite account. Leaves positions under 30% alone.")
    if st.button("Sell holdings at ≥30% profit", type="primary", use_container_width=True):
        if not kite.get("ok"):
            st.error("Connect Kite first (sidebar).")
        else:
            status = st.status("Checking holdings…", expanded=True)
            try:

                def _p(msg: str) -> None:
                    status.write(msg)

                report = auto_sell_profits(settings, min_profit_pct=PROFIT_SELL_PCT, progress=_p)
                st.session_state["sell_report"] = report
                status.update(label="Done", state="complete")
            except Exception as exc:
                status.update(label="Failed", state="error")
                st.error(str(exc))

    sell_report = st.session_state.get("sell_report")
    if sell_report:
        st.info(sell_report.get("note") or "")
        if sell_report.get("sold"):
            st.markdown("**Sold / would sell**")
            st.dataframe(pd.DataFrame(sell_report["sold"]), hide_index=True, use_container_width=True)
        if sell_report.get("kept"):
            st.markdown("**Kept (< 30%)**")
            st.dataframe(pd.DataFrame(sell_report["kept"]), hide_index=True, use_container_width=True)
        if sell_report.get("holdings") and not sell_report.get("sold") and not sell_report.get("kept"):
            st.dataframe(pd.DataFrame(sell_report["holdings"]), hide_index=True, use_container_width=True)

    st.markdown("---")
    st.subheader("1b · Manual sell (any holding)")
    st.caption(
        "Load Kite holdings, pick what to sell and how many shares. "
        "Works at any PnL — not only ≥30%."
    )
    if st.button("Load / refresh holdings", use_container_width=True):
        if not kite.get("ok"):
            st.error("Connect Kite first (sidebar).")
        else:
            try:
                st.session_state["manual_holdings"] = fetch_kite_holdings(settings)
            except Exception as exc:
                st.error(str(exc))

    holdings = st.session_state.get("manual_holdings") or []
    if holdings:
        st.dataframe(pd.DataFrame(holdings), hide_index=True, use_container_width=True)
        labels = [
            f"{h['symbol']} · qty {h['qty']} · PnL {h['pnl_pct']}% · LTP {h['ltp']}"
            for h in holdings
        ]
        by_label = {labels[i]: holdings[i] for i in range(len(holdings))}
        picked = st.multiselect("Select stocks to sell", options=labels)
        selections: list[dict] = []
        for lab in picked:
            h = by_label[lab]
            max_q = int(h["qty"])
            qty = st.number_input(
                f"Qty · {h['symbol']} (max {max_q})",
                min_value=1,
                max_value=max_q,
                value=max_q,
                step=1,
                key=f"manual_sell_qty_{h['symbol']}",
            )
            selections.append({"symbol": h["symbol"], "qty": int(qty)})

        if st.button("Sell selected", type="primary", use_container_width=True):
            if not selections:
                st.error("Select at least one stock.")
            else:
                status = st.status("Placing sells…", expanded=True)
                try:

                    def _p(msg: str) -> None:
                        status.write(msg)

                    report = manual_sell_holdings(settings, selections, progress=_p)
                    st.session_state["manual_sell_report"] = report
                    # Refresh holdings after sell
                    st.session_state["manual_holdings"] = fetch_kite_holdings(settings)
                    status.update(label="Done", state="complete")
                except Exception as exc:
                    status.update(label="Failed", state="error")
                    st.error(str(exc))
    else:
        st.caption("Click **Load / refresh holdings** to choose what to sell.")

    manual_sell_report = st.session_state.get("manual_sell_report")
    if manual_sell_report:
        st.info(manual_sell_report.get("note") or "")
        if manual_sell_report.get("sold"):
            st.dataframe(
                pd.DataFrame(manual_sell_report["sold"]),
                hide_index=True,
                use_container_width=True,
            )

    st.markdown("---")
    st.subheader("1c · Manual buy")
    st.caption(
        "Enter any NSE symbol, choose qty or ₹ amount, then place a buy. "
        "Uses Kite live LTP when connected (or enter price yourself)."
    )
    mb1, mb2, mb3 = st.columns([2, 1, 1])
    with mb1:
        mb_symbol = st.text_input("Symbol (NSE)", value="", placeholder="e.g. INFY").strip().upper()
    with mb2:
        mb_mode = st.selectbox("Size by", ["Quantity", "Capital (₹)"], index=0)
    with mb3:
        if st.button("Fetch LTP", use_container_width=True):
            if not mb_symbol:
                st.error("Enter a symbol first.")
            else:
                q = fetch_kite_quote(mb_symbol, settings)
                if q and q.get("ltp"):
                    st.session_state["manual_buy_ltp"] = float(q["ltp"])
                    st.session_state["manual_buy_quote"] = q
                else:
                    st.warning("No Kite quote — enter price manually below.")

    ltp_default = float(st.session_state.get("manual_buy_ltp") or 0.0)
    mb_price = st.number_input(
        "Entry price (₹) — 0 = use Kite LTP at order time",
        min_value=0.0,
        max_value=1_000_000.0,
        value=ltp_default,
        step=0.05,
        key="manual_buy_price_input",
    )
    if mb_mode == "Quantity":
        mb_qty = st.number_input("Quantity", min_value=1, max_value=1_000_000, value=1, step=1)
        mb_cap = None
    else:
        mb_qty = None
        mb_cap = st.number_input(
            "Capital (₹)",
            min_value=100.0,
            max_value=50_000_000.0,
            value=10_000.0,
            step=500.0,
        )

    quote_preview = st.session_state.get("manual_buy_quote")
    if quote_preview and mb_symbol:
        st.caption(
            f"{mb_symbol} Kite — LTP {quote_preview.get('ltp')} · "
            f"day {quote_preview.get('day_chg_pct')}%"
        )

    if st.button("Place manual buy", type="primary", use_container_width=True):
        if not mb_symbol:
            st.error("Enter a symbol.")
        else:
            status = st.status("Placing buy…", expanded=True)
            try:

                def _p(msg: str) -> None:
                    status.write(msg)

                report = manual_buy(
                    conn,
                    settings,
                    symbol=mb_symbol,
                    qty=int(mb_qty) if mb_qty is not None else None,
                    capital=float(mb_cap) if mb_cap is not None else None,
                    entry_price=float(mb_price) if mb_price and mb_price > 0 else None,
                    progress=_p,
                )
                st.session_state["manual_buy_report"] = report
                status.update(
                    label="Done" if report.get("ok") else "Failed",
                    state="complete" if report.get("ok") else "error",
                )
            except Exception as exc:
                status.update(label="Failed", state="error")
                st.error(str(exc))

    manual_buy_report = st.session_state.get("manual_buy_report")
    if manual_buy_report:
        st.info(manual_buy_report.get("note") or "")
        show = {
            k: manual_buy_report.get(k)
            for k in ("symbol", "qty", "price", "stop", "target", "mode", "ok")
        }
        st.dataframe(pd.DataFrame([show]), hide_index=True, use_container_width=True)

    st.markdown("---")
    st.subheader("2 · Research")
    st.caption(
        "Step A: Suggest → Step B: select stocks → Step C: place order. "
        "How many stocks = exact count suggested. Qty = capital ÷ selected ÷ price."
    )
    index_filters = st.multiselect(
        "NSE indexes — pick any / multiple",
        options=INDEX_LABELS,
        default=[i for i in DEFAULT_INDEX_FILTERS if i in INDEX_LABELS],
        help="Stock must belong to at least one selected index (from official NSE lists).",
    )
    c_ref1, c_ref2 = st.columns([3, 1])
    with c_ref1:
        st.caption("Lists come from NSE official index CSVs (archives.nseindia.com).")
    with c_ref2:
        if st.button("Refresh index lists", use_container_width=True):
            try:
                stocks = refresh_universe(conn, settings)
                st.success(f"Loaded {len(stocks)} unique names from NSE.")
            except Exception as exc:
                st.error(str(exc))
    band_labels = [b[0] for b in PRICE_BANDS]
    price_bands = st.multiselect(
        "Price range (₹) — pick any / multiple",
        options=band_labels,
        default=[b for b in DEFAULT_PRICE_BANDS if b in band_labels],
        help="Stock last price / live LTP must fall in at least one selected band.",
    )
    col_a, col_b = st.columns([2, 1])
    with col_a:
        capital = st.number_input(
            "Capital to invest (₹)",
            min_value=1_000.0,
            max_value=50_000.0,
            value=min(50_000.0, max(1_000.0, float(settings.capital))),
            step=500.0,
            help="₹1,000 – ₹50,000. Share qty = capital ÷ selected stocks ÷ price.",
        )
    with col_b:
        pick_n = st.number_input(
            "How many stocks to suggest",
            min_value=1,
            max_value=50,
            value=3,
            step=1,
            help="Exact number of suggestions (e.g. 1 → only 1 stock).",
        )

    if st.button("1 · Suggest stocks", type="primary", use_container_width=True):
        if not index_filters:
            st.error("Select at least one NSE index.")
        elif not price_bands:
            st.error("Select at least one price range.")
        else:
            status = st.status("Suggesting…", expanded=True)
            try:

                def _p(msg: str) -> None:
                    status.write(msg)

                result = research_and_buy(
                    conn,
                    settings,
                    provider,
                    capital=float(capital),
                    pick_count=int(pick_n),
                    progress=_p,
                    place_orders=False,
                    price_bands=list(price_bands),
                    index_filters=list(index_filters),
                )
                st.session_state["buy_report"] = result
                st.session_state.pop("selected_buy_report", None)
                status.update(label="Done", state="complete")
            except Exception as exc:
                status.update(label="Failed", state="error")
                st.error(str(exc))

    buy_report = st.session_state.get("buy_report")
    if buy_report:
        if settings.paper_mode:
            st.warning(
                "PAPER mode is on — place-order will **not** hit Zerodha. "
                "Turn on **Send real orders to Zerodha** in the sidebar for live buys."
            )
        st.info(buy_report.get("note") or "")
        bits = []
        if buy_report.get("pick_count") is not None:
            bits.append(f"Asked for {buy_report['pick_count']}")
        if buy_report.get("picks") is not None:
            bits.append(f"Showing {len(buy_report['picks'])}")
        if buy_report.get("price_bands"):
            bits.append("Bands: " + ", ".join(buy_report["price_bands"]))
        if bits:
            st.caption(" · ".join(bits))
        if buy_report.get("picks"):
            st.markdown(f"**Suggested ({len(buy_report['picks'])})**")
            show = []
            for p in buy_report["picks"]:
                show.append(
                    {
                        "symbol": p.get("symbol"),
                        "price": p.get("price"),
                        "qty": p.get("qty"),
                        "invest_₹": p.get("invest_₹"),
                        "slice_₹": p.get("slice_capital"),
                        "stop": p.get("stop"),
                        "target": p.get("target"),
                        "rules": p.get("rule_buys"),
                        "why": p.get("pick_note"),
                    }
                )
            st.dataframe(pd.DataFrame(show), hide_index=True, use_container_width=True)
            for p in buy_report["picks"]:
                src = p.get("data_source") or ""
                label = "Finnhub news" if "finnhub" in src else "Kite live report"
                news = p.get("news") or []
                if news:
                    with st.expander(f"{label} · {p.get('symbol')}"):
                        for n in news:
                            st.write(f"- {n}")
                q = p.get("kite_quote")
                if q:
                    st.caption(
                        f"{p.get('symbol')} Kite — LTP {q.get('ltp')} · "
                        f"O {q.get('open')} H {q.get('high')} L {q.get('low')} C {q.get('close')}"
                    )
            if buy_report.get("market_news"):
                with st.expander("Finnhub market news"):
                    for n in buy_report["market_news"]:
                        st.write(f"- {n.get('headline')}")

            st.markdown("---")
            st.subheader("3 · Select & place order")
            st.caption("Tick any of the suggested stocks, then place the order.")
            pick_syms = [str(p.get("symbol")) for p in buy_report["picks"] if p.get("symbol")]
            selected = st.multiselect(
                "Select stocks to buy",
                options=pick_syms,
                default=list(pick_syms),
                help="Only suggested names appear here. Capital is split across your selection.",
            )
            if selected:
                chosen = [p for p in buy_report["picks"] if p.get("symbol") in selected]
                sized = plan_buys_from_capital(chosen, float(capital))
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "symbol": p.get("symbol"),
                                "price": p.get("price"),
                                "qty": p.get("qty"),
                                "invest_₹": p.get("invest_₹"),
                                "slice_₹": p.get("slice_capital"),
                            }
                            for p in sized
                        ]
                    ),
                    hide_index=True,
                    use_container_width=True,
                )
                leftover = round(float(capital) - sum(float(p["invest_₹"]) for p in sized), 2)
                st.caption(
                    f"Qty from capital ₹{capital:,.0f} ÷ {len(sized)} name(s). "
                    f"Unspent ≈ ₹{leftover:,.0f}."
                )
                btn_label = (
                    "2 · Place LIVE order for selected"
                    if not settings.paper_mode
                    else "2 · Place paper order for selected (not Zerodha)"
                )
                if st.button(btn_label, type="primary", use_container_width=True):
                    if settings.paper_mode:
                        st.info("Paper run — Kite account will not change.")
                    status = st.status("Placing selected buys…", expanded=True)
                    try:

                        def _p2(msg: str) -> None:
                            status.write(msg)

                        placed = place_selected_buys(
                            conn,
                            settings,
                            buy_report["picks"],
                            selected,
                            capital=float(capital),
                            progress=_p2,
                        )
                        st.session_state["selected_buy_report"] = placed
                        status.update(label="Done", state="complete")
                    except Exception as exc:
                        status.update(label="Failed", state="error")
                        st.error(str(exc))
            else:
                st.warning("Select at least one suggested stock to place an order.")
        elif buy_report.get("in_band"):
            st.markdown("**In your price bands (did not become final picks)**")
            st.dataframe(
                pd.DataFrame(buy_report["in_band"])[
                    [c for c in ("symbol", "name", "price", "rank", "rule_buys", "rule_score", "rule_signal")
                     if c in pd.DataFrame(buy_report["in_band"]).columns]
                ],
                hide_index=True,
                use_container_width=True,
            )

    selected_buy_report = st.session_state.get("selected_buy_report")
    if selected_buy_report:
        if selected_buy_report.get("ok"):
            st.success(selected_buy_report.get("note") or "")
        else:
            st.error(selected_buy_report.get("note") or "Order failed.")
        if selected_buy_report.get("orders"):
            st.markdown("**Order result**")
            rows = []
            for o in selected_buy_report["orders"]:
                rows.append(
                    {
                        "symbol": o.get("symbol"),
                        "ok": o.get("ok"),
                        "qty": o.get("qty"),
                        "price": o.get("price"),
                        "reason": o.get("reason") or ("sent" if o.get("ok") else ""),
                        "broker_order_id": o.get("broker_order_id"),
                        "mode": o.get("mode"),
                    }
                )
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    st.markdown("---")
    st.subheader("Bot book (paper/live positions from this app)")
    opens = list_open_positions(conn)
    if opens:
        st.dataframe(pd.DataFrame(opens), hide_index=True, use_container_width=True)
    else:
        st.caption("No open bot positions yet.")


if __name__ == "__main__":
    main()
