# NSE Cash-Equity Bot — Phase 2

Python research stack for NSE cash equities: a Top 200 universe, a rule-based + AI selection filter, a single-stock backtester, and a Streamlit dashboard with Kite login. **No live orders are placed.** `PAPER_MODE` defaults to `true`.

This is not investment advice. Historical simulations are not live results.

## What is built

| Module | Status |
| --- | --- |
| 1. Stock universe + daily selection | Done |
| 2. AI decision layer | Done |
| 3. Backtest + cache | Done |
| 6. Streamlit dashboard (backtest, selection, Kite login, live quotes) | Done |
| 4. Risk gate `validate_order()` | **Phase 3** |
| 5. Paper fills / live orders | **Phase 4** |

## Kite Connect app form (the URLs it is asking for)

You do **not** need a public website. The Redirect URL is a page on **this PC** that Zerodha sends you back to after login.

On [developers.kite.trade](https://developers.kite.trade) → **Create a new app**:

| Field | Paste this |
| --- | --- |
| **Type** | **Connect** (500 credits). Do **not** pick Personal — it has no historical data and no live quotes. |
| **App name** | `sam_bot` |
| **Zerodha Client ID** | `AR5852` |
| **Redirect URL** | `http://127.0.0.1:8501/` |
| **Postback URL** | Leave **empty** |
| **Description** | Personal NSE cash-equity research and paper-trading bot. Historical data and quotes only. |

The form often shows a locked `https://` prefix. **Delete it and paste the full URL including `http://`.** Zerodha allows HTTP only for `127.0.0.1`. Do not use `https://127.0.0.1` — your dashboard is not serving TLS.

Postback is an order-status webhook. It needs a public HTTPS URL. You do not need it to log in, pull quotes, or backtest. Leave it blank.

After the app is created, copy **API key** and **API secret**. You will paste them into the dashboard (they stay in local `.env`).

Kite access tokens expire every trading day (~6 AM IST). Click **Connect Kite** on the dashboard each morning.

## Setup

Python 3.11+ recommended.

```bash
cd d:\NOT_BACKTEST
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env
```

`.env` keys (or save them from the dashboard Setup tab):

- `PAPER_MODE=true` — leave this.
- `OPENAI_API_KEY` — your existing OpenAI key.
- `KITE_API_KEY` / `KITE_API_SECRET` — from the Connect app after you create it.
- `KITE_ACCESS_TOKEN` — filled automatically after you click Connect Kite.
- `KITE_REDIRECT_URL=http://127.0.0.1:8501/` — must match the Kite form exactly.

**Never commit `.env` or real API keys to GitHub.** See [SECURITY.md](SECURITY.md). Clone → `copy .env.example .env` → fill keys locally.

## Commands

```bash
python -m trading_bot init-db

# Dashboard: save keys, Connect Kite, run backtests, see quotes
python -m trading_bot dashboard

# Daily 3–4 stock selection
python -m trading_bot select --date 2026-08-14

python -m trading_bot backtest --symbol RELIANCE --strategy combined --start 2024-01-01 --end 2025-12-31 --capital 100000

python -m trading_bot serve
```

API (after `serve`):

- `GET /health`
- `GET /universe`
- `POST /universe/refresh`
- `POST /select` `{ "date": "2026-08-14", "manual_symbol": "INFY", "use_llm": false }`
- `GET /selections?date=2026-08-14`
- `POST /backtest`
- `GET /ai-decisions?symbol=RELIANCE`
- `GET /kite/status`

Weekday scheduler: 08:45 Asia/Kolkata, selection job only.

## Selection rules

- Universe: bundled NSE Top 200 seed (ranks as of 16 Aug 2026), refreshable from NSE index CSVs.
- At least 3 of the AI picks come from the Top 100 subset when enough buy signals exist.
- At most 1 name priced below ₹50.
- A name is selected only when **both** the rule-based trend and the AI filter say `buy`.
- Manual adds are stored with `source=manual`, separate from `ai_selected`. They still must pass the Phase 3 risk gate before any order.

## Backtest notes

- Indicators (SMA/EMA/RSI/ATR/volume) are computed in Python. The LLM does not do the math.
- Default fill: signal at day *t* close → enter at day *t+1* open, with slippage + commission from `config/settings.yaml`.
- Stop-loss is attached at entry (ATR or AI `stop_loss_pct`). No simulated trade without a stop.
- Strategies:
  - `rule_based` — indicator trend only
  - `ai_filtered` — rule-based entries filtered by the AI signal
  - `combined` — both must agree; an `avoid` from either side can exit
- Historical AI uses the heuristic filter unless you pass `--use-llm` (calls the model on candidate bars; slow and costly).
- Identical parameter runs are served from SQLite cache.

Risk parameters already live in `config/settings.yaml` (`risk_per_trade_pct`, daily loss limit, max positions, no averaging down). Position sizing uses:

`qty = floor((capital * risk_per_trade_pct) / (entry_price - stop_loss_price))`

The hard `validate_order()` gate is Phase 3.

## Tests

```bash
pytest
```

## Next (do not skip)

3. Risk-management module with unit tests, wired as a hard gate on every order including manual picks.
4. Paper trading against live quotes for several weeks (`PAPER_MODE=true`).
5. Confirm SEBI / Zerodha algo registration before any live order.
6. Go live with a small fraction of capital only after paper matches backtest expectations.
