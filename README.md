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

## What data we fetch from Kite (and what we show)

When Kite is connected, the app pulls live account + market data and shows it in the UI / research report.

### 1. Account / login

| We fetch | What it means | Where you see it |
| --- | --- | --- |
| Login session | Connect your Zerodha account (OAuth) | Sidebar “Connect Kite” |
| Profile | Confirm the link works | “Kite connected” + user id |

### 2. Your holdings (for sell)

| We fetch from Kite | What we compute / show |
| --- | --- |
| Symbol (`tradingsymbol`) | Stock name in the list |
| Quantity (`quantity` + `t1_quantity`) | How many shares you can sell |
| Average buy price | Your cost |
| Last price (LTP) | Current market price |
| → **PnL %** and **PnL ₹** | `(LTP − avg) / avg` and rupee profit |

Used by: **Auto-sell ≥30%** and **Manual sell**.

### 3. Live quote / market report (per stock)

For research, suggest, and manual buy, we call Kite **quote** on `NSE:SYMBOL` and report:

| Field | Plain meaning |
| --- | --- |
| **LTP** (`last_price`) | Live last traded price — used as entry price |
| **Open / High / Low / Close** | Today’s OHLC (close here = previous close) |
| **Day change %** | How much price moved vs previous close |
| **Day range %** | High–low range vs previous close |
| **Volume** | Shares traded today |
| **Average traded price** | VWAP-style average for the day |

**What the app shows in the report**

- For each pick: `Kite live SYMBOL: LTP … · OHLC O/H/L/C · Day change …% · Volume …`
- Used to **rank** suggestions (strong day / live LTP)
- If Finnhub has **no news**, this Kite block becomes the full “live report” fallback

### 4. Orders (only when Paper mode is OFF)

| We send to Kite | Meaning |
| --- | --- |
| Market **BUY** or **SELL** | CNC cash order on NSE |
| Quantity | Shares to buy/sell |
| Response `order_id` | Broker order id shown in the result |

In **Paper mode** we still fetch holdings/quotes from Kite, but **do not** place real orders.

### 5. Optional: historical candles

If Kite is used as the history provider: daily **OHLCV** bars to compute SMA / EMA / RSI / volume (same indicators as Yahoo fallback).

---

## How stock suggestions work (simple)

No ChatGPT. The bot is like **6 checklists + live price/news**.

### Step A — Who can be considered?

1. You pick **NSE indexes** (e.g. Smallcap, Microcap, Total Market).  
2. You pick **price ranges** (e.g. 50–100, 100–200).  
3. Only stocks in those indexes **and** in those price bands are scanned.

**Indexes you can filter** (official NSE lists; union ≈ Total Market ~750):  
Nifty 50, Next 50, 100, 200, 500 · Midcap 50/100/150 · Smallcap 50/100/250 · MidSmallcap 400 · **Microcap 250** · **Smallcap250 Quality 50** · **Smallcap250 Momentum Quality 100** · **Total Market**.  
Use **Refresh index lists** in the app to update from NSE.

### Step B — Six simple rules (each votes Buy / Hold / Avoid)

Think of each rule as a question about the daily chart:

| Rule | Simple idea | Votes **Buy** when… |
| --- | --- | --- |
| **1. SMA crossover** | Short average above long average? | Price is in an uptrend on SMAs, not overbought, volume OK |
| **2. EMA crossover** | Same idea with EMAs (reacts faster) | Fast EMA above slow EMA, price above them, RSI OK |
| **3. RSI pullback** | Did the stock dip a bit in an uptrend? | Still in uptrend, but RSI cooled to about **40–55** (not chasing a spike) |
| **4. Trend quality** | Do SMA **and** EMA agree? | Both trends bullish, price above fast SMA, RSI mid-range, volume OK |
| **5. Momentum** | Is price still pushing up? | Price above both SMAs, RSI roughly **48–68** |
| **6. Volume thrust** | Is buying interest rising? | Uptrend + **higher than usual volume** |

Each rule also gives a **score 0–100**.

**How we combine them**

- Count **Buy** votes (out of 6).  
- Usually need about **2+ Buy votes** to enter the shortlist (a bit looser for very cheap bands).  
- Many **Avoid** votes → skip.  
- Sort by: more Buy votes first, then higher score.  
- Keep a shortlist of the best names.

### Step C — Live check (Kite + Finnhub)

For each shortlisted name:

1. Pull **Kite live LTP / day change** (see section above).  
2. Pull **Finnhub news** if the key is set; good/bad headlines adjust the score a little.  
3. If there is no Finnhub news → use the **Kite live report** only.  
4. Drop names whose live price left your price bands.  
5. Take the top **2 or 3**.

### Step D — What you get

| Mode | Result |
| --- | --- |
| **Suggest only** | Show symbols, price, planned qty, stop (−8%), target (+30%) — **no order** |
| **Place buy orders** | Same plan, then place buys (paper or live) |

**One-line summary:**  
*Indexes + price filter → 6 chart rules vote → Kite live price (+ optional Finnhub news) ranks the winners → suggest or buy.*

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
