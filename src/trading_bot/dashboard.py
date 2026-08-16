"""Simple Streamlit UI — two actions: sell ≥30% profits, research & buy 2–3 stocks."""

from __future__ import annotations

import os

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
from trading_bot.simple_bot import PROFIT_SELL_PCT, auto_sell_profits, research_and_buy

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
        if settings.paper_mode:
            st.success("PAPER_MODE on — simulated fills")
        else:
            st.error("PAPER_MODE off — real Zerodha orders")

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

    st.title("NSE Simple Bot")
    st.markdown(
        """
Two buttons only:

1. **Sell profits** — read your Kite holdings; if any are **≥30%** up, place a **sell**; otherwise leave them  
2. **Buy with capital** — research **2–3 medium established** stocks using:
   - Rule voters (SMA / EMA / RSI / trend / momentum / volume)  
   - **Finnhub** live company + market news  
   - **Kite** live quotes  
   - Split your money and place **buys**  

No ChatGPT / OpenAI in this flow.
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
    st.subheader("2 · Research & buy 2–3 stocks")
    st.caption(
        "Universe: medium established NSE names. Scoring: SMA/EMA/RSI/trend/momentum/volume "
        "+ Finnhub news + Kite live LTP. Capital is split equally."
    )
    col_a, col_b = st.columns([2, 1])
    with col_a:
        capital = st.number_input(
            "Capital to invest (₹)",
            min_value=5_000.0,
            max_value=50_000_000.0,
            value=float(settings.capital),
            step=5_000.0,
        )
    with col_b:
        pick_n = st.selectbox("How many stocks", [2, 3], index=1)

    if st.button("Research & place buy orders", type="primary", use_container_width=True):
        status = st.status("Researching…", expanded=True)
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
            )
            st.session_state["buy_report"] = result
            status.update(label="Done", state="complete")
        except Exception as exc:
            status.update(label="Failed", state="error")
            st.error(str(exc))

    buy_report = st.session_state.get("buy_report")
    if buy_report:
        st.info(buy_report.get("note") or "")
        if buy_report.get("picks"):
            st.markdown("**Picks**")
            show = []
            for p in buy_report["picks"]:
                show.append(
                    {
                        "symbol": p.get("symbol"),
                        "price": p.get("price"),
                        "qty": p.get("qty"),
                        "slice_₹": p.get("slice_capital"),
                        "stop": p.get("stop"),
                        "target": p.get("target"),
                        "rules": p.get("rule_buys"),
                        "why": p.get("pick_note"),
                    }
                )
            st.dataframe(pd.DataFrame(show), hide_index=True, use_container_width=True)
            for p in buy_report["picks"]:
                news = p.get("news") or []
                if news:
                    with st.expander(f"Finnhub news · {p.get('symbol')}"):
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
        if buy_report.get("orders"):
            st.markdown("**Orders**")
            st.dataframe(pd.DataFrame(buy_report["orders"]), hide_index=True, use_container_width=True)

    st.markdown("---")
    st.subheader("Bot book (paper/live positions from this app)")
    opens = list_open_positions(conn)
    if opens:
        st.dataframe(pd.DataFrame(opens), hide_index=True, use_container_width=True)
    else:
        st.caption("No open bot positions yet.")


if __name__ == "__main__":
    main()
