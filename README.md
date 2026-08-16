# NSE Simple Bot

Streamlit app for NSE cash equities (Zerodha Kite + Finnhub).  
`PAPER_MODE=true` by default. **No ChatGPT / OpenAI** in the main flow.

## What the app does

1. **Auto-sell ≥30%** — sell Kite holdings only if unrealized profit ≥ 30%  
2. **Manual sell** — pick any holding + qty and sell  
3. **Manual buy** — enter symbol, size by qty or ₹, buy at Kite LTP (or your price)  
4. **Research** — filter by price bands → **Suggest only** or **Place buy orders** (2–3 stocks)

Live app entry: `app.py` (Streamlit Cloud).

---

## What we get from the Kite API

This app uses Zerodha’s **Kite Connect** for login, live market data, holdings, and orders.

| Kite API / call | What we use it for | Fields / data we care about |
| --- | --- | --- |
| **Login / session** (`login_url`, `generate_session`) | OAuth connect in the sidebar | `access_token`, user session |
| **Profile** (`profile`) | Show “Kite connected” status | `user_id`, `user_name`, `email`, `exchanges` |
| **Holdings** (`holdings`) | Auto-sell ≥30%, manual sell list | `tradingsymbol`, `quantity` + `t1_quantity`, `average_price`, `last_price`, `exchange` → we compute PnL % / ₹ |
| **Quote** (`quote` on `NSE:SYMBOL`) | Research ranking, manual buy LTP, fallback when Finnhub has no news | `last_price` (LTP), OHLC (`open`/`high`/`low`/`close`), `volume`, `average_price`, day change % |
| **LTP** (`ltp`) | Quick last-price lookup helper | `last_price` per symbol |
| **Place order** (`place_order`) | Live BUY / SELL when `PAPER_MODE=false` | Market CNC on NSE: `transaction_type`, `quantity`, `order_type=MARKET`, `product=CNC` → `order_id` |
| **Instruments + historical** (optional data path) | Daily OHLCV for indicators / research when Kite is the history provider | Instrument token, daily bars (`open`/`high`/`low`/`close`/`volume`) |

**Paper mode:** holdings and quotes still come from Kite when connected; **orders are simulated** (not sent) until `PAPER_MODE=false`.

---

## Rules that decide suggested stocks

Research / Suggest uses **rules only** (plus Finnhub news when available, and Kite live quotes). No ChatGPT.

### 1. Universe filters (before scoring)

| Filter | Rule |
| --- | --- |
| Market-cap rank | Keep ranks **25–160** (medium established; skip mega-caps and tiny names) |
| Price bands | User multi-select: `0-50`, `50-100`, `100-200`, `200-500`, `500-1000`, `1000-5000` (₹). Stock must fall in **at least one** selected band |
| Enough history | Need enough daily bars for indicators (SMA slow + buffer) |

### 2. Six rule voters (each votes `buy` / `hold` / `avoid`)

| Voter | Buys when (simplified) |
| --- | --- |
| **SMA crossover** | Fast SMA > slow SMA, price above fast SMA, RSI not extreme, volume supportive |
| **EMA crossover** | Fast EMA > slow EMA, price above fast EMA, not below slow SMA, RSI OK |
| **RSI pullback** | Uptrend (fast SMA > slow), price not below slow SMA, RSI cooled into **40–55** |
| **Trend quality** | Both SMA and EMA stacks bullish, price ≥ fast SMA, RSI roughly **45–65**, volume OK |
| **Momentum** | Price above both SMAs, RSI roughly **48–68**, not overextended |
| **Volume thrust** | Bullish SMA+EMA stack, elevated `volume_ratio`, RSI not overbought |

Each voter also returns a **0–100 score**. Combined:

- Count how many vote **`buy`**
- Research shortlist needs roughly **≥2 buy votes** (and score not too weak)
- Too many **`avoid`** votes → skipped

Shortlist is sorted by `(buy_vote_count, rule_score)` and the top names go to ranking.

### 3. Final ranking (pick 2–3)

For each shortlisted name:

1. **Kite live quote** — prefer live LTP; day change can boost / trim the score  
2. **Finnhub news** (if key set and articles exist) — sentiment adjusts the combined score  
3. If Finnhub is empty → **Kite-only fallback** (live OHLC / day change as context)  
4. Re-check live price still matches selected **price bands**  
5. Take top **2 or 3** by `combined_score`

### 4. Position plan (suggest or buy)

| Item | Value |
| --- | --- |
| Capital split | Equal across final picks |
| Stop | **−8%** from entry |
| Target | **+30%** from entry |
| **Suggest only** | Show picks + planned qty — **no orders** |
| **Place buy orders** | Same plan, then place buys (paper or live) |

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
