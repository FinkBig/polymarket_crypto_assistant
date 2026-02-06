# Polymarket Crypto Assistant

Real-time dashboard that combines live Binance order flow with Polymarket prediction market prices to surface actionable crypto signals.

**[Live Demo](https://polymarket-crypto-assistant.onrender.com)** | **[Polymarket](https://polymarket.com/?via=Yxwa280)**

**By [@SolSt1ne](https://x.com/SolSt1ne)**

---

## What it does

- Streams live trades and orderbook from **Binance**
- Fetches Up/Down contract prices from **Polymarket** via WebSocket
- Calculates 11 indicators across orderbook, flow, and technical analysis
- Aggregates everything into a single **BULLISH / BEARISH / NEUTRAL** trend score
- Available as both **terminal dashboard** and **web UI**

---

## Supported coins & timeframes

| Coins | Timeframes |
|-------|------------|
| BTC, ETH, SOL, XRP | 15m, 1h, 4h, daily |

All 16 coin × timeframe combinations are supported on Polymarket.

---

## Screenshots

### Web UI
Professional dark-themed interface showing all 4 timeframes for the selected coin with real-time WebSocket updates.

### Terminal Mode
Rich terminal dashboard with live refresh.

---

## Indicators

**Order Book**
- OBI (Order Book Imbalance)
- Buy / Sell Walls
- Liquidity Depth (0.1% / 0.5% / 1.0%)

**Flow & Volume**
- CVD (Cumulative Volume Delta) — 1m / 3m / 5m
- Delta (1m)
- Volume Profile with POC

**Technical Analysis**
- RSI (14)
- MACD (12/26/9) + Signal + Histogram
- VWAP
- EMA 5 / EMA 20 crossover
- Heikin Ashi candle streak

---

## How to Use for Trading

1. **Find high-confidence setups**: Look for Score +5 or higher (bullish) or -5 or lower (bearish)
2. **Check the edge**: Compare your signal against PM odds - value is when indicators disagree with market pricing
3. **Multiple timeframes agreeing**: Even stronger conviction when 15m, 1h, 4h all show same trend

| Your Signal | PM Odds | Action |
|-------------|---------|--------|
| Score +6 (bullish) | UP at 0.75 | Skip - already priced in |
| Score +6 (bullish) | UP at 0.45 | Good edge - buy UP |
| Score -5 (bearish) | DOWN at 0.40 | Good edge - buy DOWN |

---

## Setup

```bash
# Clone the repo
git clone https://github.com/FinkBig/polymarket_crypto_assistant.git
cd polymarket_crypto_assistant

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

---

## Usage

### Web UI (recommended)
```bash
python web_main.py
```
Open http://localhost:8080 in your browser.

Features:
- All 4 coins accessible via tabs
- All 4 timeframes displayed simultaneously
- Real-time updates every 10 seconds via WebSocket
- Auto-refreshes Polymarket tokens when markets expire

### Terminal Mode
```bash
python main.py
```
Interactive menu to select coin and timeframe.

---

## Project structure

```
polymarket-assistant/
├── src/
│   ├── config.py          # all constants — coins, URLs, indicator params
│   ├── feeds.py           # Binance + Polymarket data feeds
│   ├── indicators.py      # pure indicator calculations
│   ├── dashboard.py       # Rich terminal UI & trend scoring
│   └── web/
│       ├── server.py      # FastAPI server with WebSocket
│       └── static/
│           └── index.html # Web UI (single file)
├── main.py                # Terminal mode entry point
├── web_main.py            # Web UI entry point
└── requirements.txt       # Python dependencies
```

---

## Tech Stack

- **Data**: Binance REST + WebSocket, Polymarket Gamma API + WebSocket
- **Backend**: FastAPI + uvicorn (async)
- **Frontend**: Vanilla HTML/CSS/JS (no build step)
- **Terminal**: Rich library
- **Hosting**: Render.com

---

## Deploy to Render

1. Fork this repo
2. Go to [Render Dashboard](https://dashboard.render.com)
3. Click **New** → **Web Service**
4. Connect your GitHub repo
5. Render will auto-detect `render.yaml` and configure everything
6. Click **Create Web Service**

The app will be live at `https://your-service-name.onrender.com`

---

## License

MIT
