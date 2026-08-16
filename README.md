# NSE Simple Bot

Personal NSE cash-equity helper with **two buttons**:

1. **Sell ≥30% profits** — reads your Zerodha Kite holdings; sells only names up ≥30%; leaves the rest  
2. **Research & buy** — you set capital → bot picks **2–3 medium established** stocks using:
   - Rule voters: SMA, EMA, RSI, trend, momentum, volume  
   - OpenAI + latest news headlines  
   - Splits your capital equally and places buys  

`PAPER_MODE=true` by default (simulated fills). Same secrets as before (`.env` / Streamlit Secrets).

This is not investment advice.

---

## Run locally

```bash
cd d:\NOT_BACKTEST
.venv\Scripts\activate
python -m trading_bot dashboard
```

Open `http://127.0.0.1:8501/` → Connect Kite (sidebar) → use the two buttons.

## Streamlit Cloud

- Main file: `app.py`  
- Secrets: `KITE_API_KEY`, `KITE_API_SECRET`, `KITE_REDIRECT_URL`, `OPENAI_API_KEY`, `PAPER_MODE`  
- Redirect URL must match your `https://….streamlit.app/`  

## Live money

Set `PAPER_MODE=false` only when you are ready. Live sells/buys go to Zerodha. Algo trading has compliance requirements.

## CLI (optional)

```bash
python -m trading_bot take-profits
python -m trading_bot auto-trade --strategy small_swing --capital 100000
```
