from __future__ import annotations

import os
from datetime import date, timedelta

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from trading_bot.backtest import backtest
from trading_bot.strategies import PRIMARY_STRATEGIES, STRATEGY_LABELS
from trading_bot.auto_trade import run_daily_auto_trade
from trading_bot.execution import list_open_positions, manage_open_positions
from trading_bot.config import ROOT, load_settings
from trading_bot.data_provider import as_date
from trading_bot.export_backtest import (
    equity_frame,
    summary_frame,
    to_csv_bytes,
    trades_frame,
    try_save_csv_files,
)
from trading_bot.kite_auth import (
    DEFAULT_REDIRECT_URL,
    clear_session,
    exchange_request_token,
    fetch_ltp,
    kite_redirect_url,
    login_url,
    profile_status,
    upsert_env,
)
from trading_bot.runtime import boot, build_provider
from trading_bot.selection import list_selections, run_daily_selection
from trading_bot.universe import get_universe

# set_page_config lives in app.py (must be first Streamlit call on Cloud).
# Local `python -m trading_bot dashboard` still needs it here when this file is the entry.
try:
    st.set_page_config(page_title="NSE Equity Bot", layout="wide")
except st.errors.StreamlitAPIException:
    pass


def _restore_kite_token_from_session() -> None:
    """Streamlit Cloud cannot rely on .env; keep the daily token in session_state."""
    token = st.session_state.get("kite_access_token")
    if token:
        os.environ["KITE_ACCESS_TOKEN"] = str(token)


def _boot():
    # Never override=True — that wipes Streamlit Secrets already loaded into os.environ.
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
        st.error(f"Kite login did not succeed (status={status}).")
        st.query_params.clear()
        return
    try:
        session = exchange_request_token(str(token), settings)
        access = session.get("access_token")
        if access:
            st.session_state["kite_access_token"] = access
            os.environ["KITE_ACCESS_TOKEN"] = str(access)
        load_dotenv(ROOT / ".env", override=False)
        st.query_params.clear()
        st.session_state["kite_flash"] = (
            f"Kite connected as {session.get('user_name') or session.get('user_id')}."
        )
        st.rerun()
    except Exception as exc:
        st.query_params.clear()
        st.error(f"Could not exchange the Kite request token: {exc}")


def _setup_tab(settings) -> None:
    st.subheader("Streamlit Cloud deploy")
    st.markdown(
        """
1. Go to [share.streamlit.io](https://share.streamlit.io) → **New app**
2. Repo: `shaikssam65/NOT_BACKTEST` · Branch: `master` · **Main file path:** `app.py`
3. **Advanced settings → Secrets** — paste the block from `.streamlit/secrets.toml.example` with your real keys
4. Deploy, copy your app URL (looks like `https://xxxx.streamlit.app/`)
5. On [developers.kite.trade/apps](https://developers.kite.trade/apps) → **sam_bot** → set **Redirect URL** to that exact HTTPS URL (with trailing `/`)
6. Put the same URL in Streamlit Secrets as `KITE_REDIRECT_URL`, then reboot the app
"""
    )
    cloud_redirect = st.text_input(
        "Your Streamlit app URL (for Kite Redirect URL + secrets)",
        value=settings.kite_redirect_url
        if settings.kite_redirect_url.startswith("https://")
        else "https://YOUR-APP-NAME.streamlit.app/",
        help="Must match Kite Connect Redirect URL exactly.",
    )
    if st.button("Use this URL as KITE_REDIRECT_URL"):
        url = cloud_redirect.strip().rstrip("/") + "/"
        upsert_env({"KITE_REDIRECT_URL": url})
        st.success(f"Saved redirect URL: {url}")
        st.rerun()

    st.divider()
    st.subheader("1. Create the Kite app (get API key + secret)")
    st.link_button(
        "Open Kite Connect → Create app / get API keys",
        "https://developers.kite.trade/apps",
        type="primary",
    )
    st.caption(
        "After you create the app, the app page shows **API key** and **API secret**. "
        "Copy both and paste them in step 2 below (local) or Streamlit Secrets (cloud)."
    )

    redirect = kite_redirect_url(settings)
    st.markdown("### Fill the Create / Edit app form")
    st.code(
        f"""Type:              Connect
App name:          sam_bot
Zerodha Client ID: AR5852
Redirect URL:      {redirect}
Postback URL:      (leave empty)
Description:       Personal NSE cash-equity research and paper-trading bot.""",
        language=None,
    )
    st.text_input(
        "Copy this Redirect URL into the Kite form",
        value=redirect,
        key="copy_redirect_url",
    )
    if redirect.startswith("https://") and "streamlit.app" in redirect:
        st.success("Using HTTPS Streamlit URL — correct for Cloud deploy.")
    else:
        st.error(
            "Local mode uses `http://127.0.0.1:8501/`. "
            "For Streamlit Cloud, change Redirect URL to your `https://….streamlit.app/` URL."
        )
    st.warning("**Postback URL:** leave blank.")

    st.divider()
    st.subheader("2. Save API keys here (local) or use Streamlit Secrets (cloud)")
    st.caption(
        "OpenAI: [platform.openai.com/api-keys](https://platform.openai.com/api-keys) · "
        "Kite: [developers.kite.trade/apps](https://developers.kite.trade/apps)"
    )
    openai_in = st.text_input("OpenAI API key", type="password", placeholder="sk-...")
    kite_key = st.text_input("Kite API key", type="password", placeholder="from your Kite app page")
    kite_secret = st.text_input("Kite API secret", type="password", placeholder="from your Kite app page")
    if st.button("Save keys", type="primary"):
        updates: dict[str, str] = {"PAPER_MODE": "true"}
        # Keep Cloud redirect if already set; only default to localhost when unset.
        if not (os.getenv("KITE_REDIRECT_URL") or "").startswith("https://"):
            updates["KITE_REDIRECT_URL"] = DEFAULT_REDIRECT_URL
        if openai_in.strip():
            updates["OPENAI_API_KEY"] = openai_in.strip()
        if kite_key.strip():
            updates["KITE_API_KEY"] = kite_key.strip()
        if kite_secret.strip():
            updates["KITE_API_SECRET"] = kite_secret.strip()
        upsert_env(updates)
        st.success("Saved. On Streamlit Cloud, prefer App → Settings → Secrets for permanent keys.")
        st.rerun()

    settings = load_settings()
    c1, c2, c3 = st.columns(3)
    c1.metric("OpenAI", "ready" if settings.openai_ready else "missing")
    c2.metric("Kite API key", "saved" if settings.kite_api_key else "missing")
    c3.metric("Kite API secret", "saved" if settings.kite_api_secret else "missing")

    st.divider()
    st.subheader("3. Connect Kite (daily login)")
    status = profile_status(settings)
    if status.get("ok"):
        st.success(
            f"Connected as **{status.get('user_name') or status.get('user_id')}** "
            f"({status.get('user_id')}). Access tokens expire every trading day (~6 AM IST)."
        )
        if st.button("Disconnect Kite"):
            clear_session()
            st.session_state.pop("kite_access_token", None)
            os.environ.pop("KITE_ACCESS_TOKEN", None)
            st.rerun()
    else:
        reason = status.get("reason")
        if reason == "missing_api_key":
            st.warning("Save the Kite API key and secret first, then connect.")
        elif reason == "token_invalid_or_expired":
            st.warning("Saved token is invalid or expired. Login again.")
        else:
            st.info("Not connected. Start the dashboard first, then click Connect Kite.")
        if settings.kite_api_key and settings.kite_api_secret:
            st.link_button("Connect Kite", login_url(settings.kite_api_key), type="primary")
            st.caption(
                f"After login, Zerodha sends you back to `{kite_redirect_url(settings)}` "
                "with a request_token. This page exchanges it automatically."
            )


def _backtest_tab(settings, conn, provider) -> None:
    st.subheader("Run a backtest")
    universe = get_universe(conn)
    symbols = [s.symbol for s in universe]
    default_end = date.today()
    default_start = default_end - timedelta(days=365)
    c1, c2, c3, c4 = st.columns(4)
    strategy_options = list(PRIMARY_STRATEGIES)
    default_ix = 0
    symbol = c1.selectbox("Stock", symbols, index=symbols.index("RELIANCE") if "RELIANCE" in symbols else 0)
    strategy = c2.selectbox(
        "Strategy",
        strategy_options,
        index=default_ix,
        format_func=lambda s: STRATEGY_LABELS.get(s, s),
        help="Only two modes: 6 rule voters, or 2 AI agents.",
    )
    start = c3.date_input("Start", value=default_start)
    end = c4.date_input("End", value=default_end)
    capital = st.number_input("Capital (₹)", min_value=10000.0, value=float(settings.capital), step=10000.0)
    use_llm = st.checkbox(
        "Use live OpenAI on each candidate bar (slow and costly). Leave off for the heuristic AI filter.",
        value=False,
    )
    kite_ok = bool(profile_status(settings).get("ok"))
    if kite_ok:
        st.success("Kite is connected → backtest will **prefer Kite history** (Yahoo only if Kite fails).")
    else:
        st.warning("Kite is **not** connected → backtest will use **Yahoo Finance** history.")

    if st.button("Run backtest", type="primary"):
        with st.spinner(f"Backtesting {symbol} / {strategy}…"):
            try:
                result = backtest(
                    symbol,
                    strategy,
                    start,
                    end,
                    float(capital),
                    settings,
                    provider,
                    conn,
                    use_llm=use_llm,
                )
                payload = result.to_dict()
                src = provider.cache.source_summary(symbol, as_date(start), as_date(end))
                payload["price_data_source"] = src["label"]
                payload["kite_bars"] = src["kite_bars"]
                payload["yahoo_bars"] = src["yahoo_bars"]
                st.session_state["backtest"] = payload
                st.session_state["backtest_export_paths"] = try_save_csv_files(payload)
            except Exception as exc:
                st.error(str(exc))
                return

    payload = st.session_state.get("backtest")
    if not payload:
        st.caption("Pick a name and date range, then run. Repeated identical runs are served from cache.")
        return

    source = str(payload.get("price_data_source") or "unknown")
    kite_bars = int(payload.get("kite_bars") or 0)
    yahoo_bars = int(payload.get("yahoo_bars") or 0)
    if source == "kite":
        st.success(f"**Price data source: Kite** ({kite_bars} bars in cache for this range).")
    elif source == "yahoo":
        st.warning(f"**Price data source: Yahoo** ({yahoo_bars} bars in cache for this range).")
    elif source == "mixed":
        st.info(
            f"**Price data source: mixed** — Kite bars: {kite_bars}, Yahoo bars: {yahoo_bars}. "
            "Older cache may mix providers."
        )
    else:
        st.caption("Price data source: unknown (no OHLCV cache rows yet).")

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Total return", f"{payload['total_return_pct']:.2f}%")
    m2.metric("Win rate", f"{payload['win_rate']:.1f}%")
    m3.metric("Max drawdown", f"{payload['max_drawdown_pct']:.2f}%")
    m4.metric("Trades", payload["number_of_trades"])
    m5.metric("Ending equity", f"₹{payload['ending_equity']:,.0f}")
    st.info(payload.get("commentary") or "")
    curve = payload.get("equity_curve") or []
    if curve:
        frame = pd.DataFrame(curve)
        frame["date"] = pd.to_datetime(frame["date"])
        st.line_chart(frame.set_index("date")["equity"], height=280)
    trades = payload.get("trades") or []
    if trades:
        st.dataframe(pd.DataFrame(trades), hide_index=True, use_container_width=True)
    if payload.get("cached"):
        st.caption("Served from SQLite cache.")

    st.subheader("Download CSV")
    st.caption(
        "On Streamlit Cloud, use these download buttons — files are not pushed to GitHub. "
        "Results are also cached in SQLite for the same inputs."
    )
    symbol = str(payload.get("symbol") or "backtest")
    strategy = str(payload.get("strategy_name") or "strategy")
    d1, d2, d3 = st.columns(3)
    d1.download_button(
        "Summary CSV",
        data=to_csv_bytes(summary_frame(payload)),
        file_name=f"{symbol}_{strategy}_summary.csv",
        mime="text/csv",
        key="dl_summary",
    )
    d2.download_button(
        "Trades CSV",
        data=to_csv_bytes(trades_frame(payload)),
        file_name=f"{symbol}_{strategy}_trades.csv",
        mime="text/csv",
        key="dl_trades",
    )
    d3.download_button(
        "Equity curve CSV",
        data=to_csv_bytes(equity_frame(payload)),
        file_name=f"{symbol}_{strategy}_equity.csv",
        mime="text/csv",
        key="dl_equity",
    )
    paths = st.session_state.get("backtest_export_paths") or {}
    if paths:
        st.caption("Also saved on server disk (local/ephemeral): " + ", ".join(paths.values()))


def _auto_trade_tab(settings, conn, provider) -> None:
    st.subheader("Daily auto-trade")
    st.markdown(
        """
**Two decision modes only:**  
1. **Rules combo** — 6 rule voters (SMA, EMA, RSI, trend, momentum, volume)  
2. **Dual agents** — Agent-Trend + Agent-Risk must both buy  

Exits only on stop or target. Run each market day for new entries.
"""
    )
    if settings.paper_mode:
        st.success("PAPER_MODE is ON — orders are paper fills only (safe to try).")
    else:
        st.error("PAPER_MODE is OFF — this can send **live** Zerodha orders. Only for registered algo use.")

    default_idx = list(PRIMARY_STRATEGIES).index("rules_combo")
    strategy = st.selectbox(
        "Decision mode",
        list(PRIMARY_STRATEGIES),
        index=default_idx,
        format_func=lambda s: STRATEGY_LABELS.get(s, s),
        key="auto_strategy",
    )
    trade_capital = st.number_input(
        "Capital to use for trading (₹)",
        min_value=10_000.0,
        max_value=50_000_000.0,
        value=float(settings.capital),
        step=10_000.0,
        key="auto_capital",
        help="Used for position sizing and risk checks. Does not withdraw money by itself.",
    )
    use_llm = st.checkbox(
        "Use OpenAI for Dual agents (ignored for Rules combo; falls back to heuristics if off)",
        value=settings.openai_ready,
        key="auto_llm",
    )
    c1, c2 = st.columns(2)
    if c1.button("Run daily auto-trade now", type="primary"):
        status = st.status("Running auto-trade…", expanded=True)
        try:

            def _progress(msg: str) -> None:
                status.write(msg)

            result = run_daily_auto_trade(
                conn,
                settings,
                provider,
                strategy=strategy,
                use_llm=use_llm,
                manage_exits=True,
                trade_capital=float(trade_capital),
                progress=_progress,
            )
            st.session_state["auto_trade"] = result
            status.update(label="Auto-trade finished", state="complete")
        except Exception as exc:
            status.update(label="Auto-trade failed", state="error")
            st.error(str(exc))
            return
    if c2.button("Manage exits only (check SL/target vs LTP)"):
        try:
            actions = manage_open_positions(conn, settings)
            st.session_state["exit_actions"] = actions
            st.info(f"Checked {len(actions)} open position(s).")
        except Exception as exc:
            st.error(str(exc))

    result = st.session_state.get("auto_trade")
    if result:
        st.markdown("---")
        st.markdown(
            f"**Mode:** `{result['mode']}` · **Style:** `{result.get('style', 'daily')}` · "
            f"**Strategy:** `{result['strategy']}` · "
            f"**Capital:** ₹{result.get('trade_capital', 0):,.0f} · "
            f"**Scanned:** {result.get('scanned', 0)} · "
            f"**Picks:** {', '.join(result.get('picks') or []) or 'none'}"
        )
        st.caption(result.get("note") or "")

        with st.expander("Activity log (what happened)", expanded=True):
            for line in result.get("activity") or []:
                st.text(line)

        detected = result.get("detected") or []
        st.markdown("### Stocks detected (buy candidates)")
        if detected:
            st.dataframe(pd.DataFrame(detected), hide_index=True, use_container_width=True)
        else:
            st.caption("No buy candidates today — rules/agents did not agree. That is normal.")

        if result.get("votes"):
            with st.expander("Ensemble votes (detail)", expanded=False):
                vote_rows = []
                for v in result["votes"]:
                    rv = v.get("rule_votes") or {}
                    vote_rows.append(
                        {
                            "symbol": v.get("symbol"),
                            "final": v.get("final_signal"),
                            "score": v.get("final_score"),
                            "rule_buys": v.get("rule_buy_count"),
                            "sma": rv.get("sma_crossover"),
                            "ema": rv.get("ema_crossover"),
                            "rsi": rv.get("rsi_pullback"),
                            "trend": rv.get("trend_quality"),
                            "agent_trend": (v.get("agent_trend") or {}).get("signal"),
                            "agent_risk": (v.get("agent_risk") or {}).get("signal"),
                            "stop_%": v.get("stop_loss_pct"),
                            "target_%": v.get("target_pct"),
                            "reasoning": v.get("reasoning"),
                        }
                    )
                st.dataframe(pd.DataFrame(vote_rows), hide_index=True, use_container_width=True)

        st.markdown("### Orders placed")
        if result.get("orders"):
            st.dataframe(pd.DataFrame(result["orders"]), hide_index=True, use_container_width=True)
        else:
            st.caption("No orders placed.")

        plans = result.get("entry_plans") or []
        st.markdown("### Entry & exit plan")
        if plans:
            st.dataframe(pd.DataFrame(plans), hide_index=True, use_container_width=True)
            for p in plans:
                st.info(
                    f"**{p['symbol']}** — buy {p.get('qty')} @ ₹{p.get('entry')} · "
                    f"exit stop ₹{p.get('stop_loss')} / target ₹{p.get('target')} · "
                    f"{p.get('exit_when')}"
                )
        else:
            st.caption("No filled entries this run.")

        if result.get("exits"):
            st.markdown("### Exit checks this run")
            st.dataframe(pd.DataFrame(result["exits"]), hide_index=True, use_container_width=True)

    opens = list_open_positions(conn)
    st.markdown("### Open positions (live book)")
    if opens:
        enriched = []
        for p in opens:
            entry = float(p.get("entry_price") or 0)
            stop = float(p.get("stop_loss") or 0)
            target = p.get("target")
            enriched.append(
                {
                    **dict(p),
                    "exit_plan": (
                        f"SELL ≤ {stop:.2f} (SL)"
                        + (f" or ≥ {float(target):.2f} (target)" if target is not None else "")
                    ),
                }
            )
        st.dataframe(pd.DataFrame(enriched), hide_index=True, use_container_width=True)
    else:
        st.caption("No open positions.")

    exits = st.session_state.get("exit_actions")
    if exits and not result:
        st.markdown("**Last exit check**")
        st.dataframe(pd.DataFrame(exits), hide_index=True, use_container_width=True)


def _selection_tab(settings, conn, provider) -> None:
    st.subheader("Daily selection")
    as_of = st.date_input("Selection date", value=date.today(), key="sel_date")
    manual = st.text_input("Optional manual symbol (tagged separately, still no live orders)", placeholder="INFY")
    use_llm = st.checkbox("Use OpenAI for the filter", value=settings.openai_ready)
    if st.button("Run selection", type="primary"):
        with st.spinner("Scoring universe… this can take a few minutes on the first Kite/Yahoo download."):
            try:
                picks = run_daily_selection(
                    conn,
                    settings,
                    provider,
                    as_of=as_of,
                    manual_symbol=manual.strip() or None,
                    use_llm=use_llm,
                )
                st.session_state["picks"] = [p.to_record(as_of.isoformat()) | {"name": p.stock.name} for p in picks]
            except Exception as exc:
                st.error(str(exc))
                return

    rows = st.session_state.get("picks") or list_selections(conn, as_of.isoformat())
    if not rows:
        st.caption("No selection stored for this date yet.")
        return
    frame = pd.DataFrame(rows)
    if "source" in frame.columns:
        st.dataframe(frame, hide_index=True, use_container_width=True)
        ai_rows = frame[frame["source"] == "ai_selected"] if "source" in frame else frame
        manual_rows = frame[frame["source"] == "manual"] if "source" in frame else frame.iloc[0:0]
        st.caption(f"AI-selected: {len(ai_rows)} · Manual: {len(manual_rows)}")
    else:
        st.dataframe(frame, hide_index=True, use_container_width=True)


def _quotes_tab(settings, conn) -> None:
    st.subheader("Live quotes")
    status = profile_status(settings)
    if not status.get("ok"):
        st.warning("Connect Kite on the Setup tab to see live NSE last prices. PAPER_MODE is still on — quotes only, no orders.")
        return

    stored = list_selections(conn, date.today().isoformat())
    default_symbols = [row["symbol"] for row in stored] or ["RELIANCE", "HDFCBANK", "TCS", "INFY"]
    universe = get_universe(conn)
    options = [s.symbol for s in universe]
    chosen = st.multiselect("Symbols", options, default=[s for s in default_symbols if s in options][:8])
    if st.button("Refresh quotes") or chosen:
        if not chosen:
            return
        try:
            prices = fetch_ltp(chosen, settings)
        except Exception as exc:
            st.error(f"Quote fetch failed: {exc}")
            return
        if not prices:
            st.warning("No prices returned. Confirm the Connect app is active and you completed today's login.")
            return
        frame = pd.DataFrame(
            [{"symbol": sym, "ltp": prices.get(sym)} for sym in chosen]
        )
        st.dataframe(frame, hide_index=True, use_container_width=True)
        cols = st.columns(min(4, len(frame)))
        for i, row in enumerate(frame.itertuples(index=False)):
            cols[i % len(cols)].metric(row.symbol, f"₹{row.ltp:,.2f}" if row.ltp is not None else "—")


def main() -> None:
    settings, conn, provider = _boot()
    _consume_kite_callback(settings)
    _restore_kite_token_from_session()
    settings = load_settings()

    st.title("NSE cash-equity bot")
    st.caption("Phase 2 dashboard — backtests, selection audit, and Kite login. No live orders.")
    if settings.paper_mode:
        st.success("PAPER_MODE is on. Connecting Kite only authorizes data and (later) paper fills — not live orders.")
    else:
        st.error("PAPER_MODE is off. Turn it back to true until Phase 4–6 are done.")

    flash = st.session_state.pop("kite_flash", None)
    if flash:
        st.success(flash)

    c1, c2, c3, c4 = st.columns(4)
    kite = profile_status(settings)
    c1.metric("Paper mode", "on" if settings.paper_mode else "OFF")
    c2.metric("OpenAI", "ready" if settings.openai_ready else "missing")
    c3.metric("Kite", "connected" if kite.get("ok") else "not connected")
    c4.metric("Universe", len(get_universe(conn)))

    if kite.get("ok"):
        st.info(
            "Kite connected. Open **Backtest** (prefers Kite history) or **Daily selection**. "
            "Still no live orders while PAPER_MODE is on."
        )
    else:
        st.warning(
            "**Kite is not connected yet.** Open **Setup & Kite** → **Connect Kite**, "
            "then use **Backtest** / **Auto-trade**. Paper auto-trade works without live orders."
        )
        if settings.kite_api_key and settings.kite_api_secret:
            st.link_button(
                "Connect Kite now",
                login_url(settings.kite_api_key),
                type="primary",
            )
        else:
            st.error(
                "Put `KITE_API_KEY` and `KITE_API_SECRET` in Streamlit **Secrets**, then reboot. "
                "On Cloud, Secrets are required — the on-page Save form is not enough alone."
            )

    redirect = kite_redirect_url(settings)
    on_cloud = (
        os.getenv("STREAMLIT_RUNTIME_ENV", "").lower() == "cloud"
        or "STREAMLIT_SHARING_MODE" in os.environ
    )
    if "YOUR-APP-NAME" in redirect or (on_cloud and not redirect.startswith("https://")):
        st.error(
            'Set `KITE_REDIRECT_URL = "https://backtestind.streamlit.app/"` in Streamlit Secrets '
            "and the same URL on the Kite app, then reboot."
        )

    tab_setup, tab_bt, tab_sel, tab_auto, tab_px = st.tabs(
        ["Setup & Kite", "Backtest", "Daily selection", "Auto-trade", "Live quotes"]
    )
    with tab_setup:
        _setup_tab(settings)
    with tab_bt:
        _backtest_tab(settings, conn, provider)
    with tab_sel:
        _selection_tab(settings, conn, provider)
    with tab_auto:
        _auto_trade_tab(settings, conn, provider)
    with tab_px:
        _quotes_tab(settings, conn)


if __name__ == "__main__":
    main()
