from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from trading_bot.backtest import VALID_STRATEGIES, backtest
from trading_bot.config import ROOT, load_settings
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

st.set_page_config(page_title="NSE Equity Bot", layout="wide")


def _boot():
    load_dotenv(ROOT / ".env", override=True)
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
        load_dotenv(ROOT / ".env", override=True)
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
        load_dotenv(ROOT / ".env", override=True)
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
        updates: dict[str, str] = {"PAPER_MODE": "true", "KITE_REDIRECT_URL": DEFAULT_REDIRECT_URL}
        if openai_in.strip():
            updates["OPENAI_API_KEY"] = openai_in.strip()
        if kite_key.strip():
            updates["KITE_API_KEY"] = kite_key.strip()
        if kite_secret.strip():
            updates["KITE_API_SECRET"] = kite_secret.strip()
        upsert_env(updates)
        load_dotenv(ROOT / ".env", override=True)
        st.success("Saved. Click Connect Kite next.")
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
            load_dotenv(ROOT / ".env", override=True)
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
    symbol = c1.selectbox("Stock", symbols, index=symbols.index("RELIANCE") if "RELIANCE" in symbols else 0)
    strategy = c2.selectbox("Strategy", list(VALID_STRATEGIES), index=2)
    start = c3.date_input("Start", value=default_start)
    end = c4.date_input("End", value=default_end)
    capital = st.number_input("Capital (₹)", min_value=10000.0, value=float(settings.capital), step=10000.0)
    use_llm = st.checkbox(
        "Use live OpenAI on each candidate bar (slow and costly). Leave off for the heuristic AI filter.",
        value=False,
    )
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
                st.session_state["backtest"] = result.to_dict()
            except Exception as exc:
                st.error(str(exc))
                return

    payload = st.session_state.get("backtest")
    if not payload:
        st.caption("Pick a name and date range, then run. Repeated identical runs are served from cache.")
        return

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

    tab_setup, tab_bt, tab_sel, tab_px = st.tabs(
        ["Setup & Kite", "Backtest", "Daily selection", "Live quotes"]
    )
    with tab_setup:
        _setup_tab(settings)
    with tab_bt:
        _backtest_tab(settings, conn, provider)
    with tab_sel:
        _selection_tab(settings, conn, provider)
    with tab_px:
        _quotes_tab(settings, conn)


main()
