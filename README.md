# NSE Simple Bot

Two buttons:

1. **Sell ≥30% profits** — Kite holdings; sell only if ≥30% up  
2. **Research & buy** — 2–3 medium established stocks using:
   - Rules: SMA, EMA, RSI, trend, momentum, volume  
   - **Finnhub** news  
   - **Kite** live quotes  
   - Equal capital split → buy orders  

**No ChatGPT / OpenAI** in this flow.  
`PAPER_MODE=true` by default. Same repo secrets pattern.

---

## Secrets

| Key | Purpose |
| --- | --- |
| `KITE_API_KEY` / `KITE_API_SECRET` / `KITE_ACCESS_TOKEN` | Kite login, quotes, holdings, orders |
| `KITE_REDIRECT_URL` | OAuth redirect |
| `FINNHUB_API_KEY` | Live company + market news |
| `PAPER_MODE` | `true` = paper fills (default) |

Get a free Finnhub key: https://finnhub.io/

## Run

```bash
cd d:\NOT_BACKTEST
.venv\Scripts\activate
python -m trading_bot dashboard
```

Streamlit Cloud: main file `app.py`; put the secrets above in **Secrets**.
