import asyncio
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# Add src directory to Python path
_src_dir = Path(__file__).parent.parent
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

import config

# HTTP session with headers to avoid blocks
http_session = requests.Session()
http_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
})

STATIC_DIR = Path(__file__).parent / "static"

# Simple cache
_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 10  # seconds

# WebSocket clients
connected_clients: list[WebSocket] = []
broadcast_task = None


def get_cached(key: str):
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
    return None


def set_cached(key: str, data: Any):
    _cache[key] = (time.time(), data)


# ── Binance Data Fetching ──────────────────────────────────────────

def fetch_binance_orderbook(symbol: str) -> dict:
    """Fetch orderbook from Binance."""
    try:
        resp = http_session.get(
            f"{config.BINANCE_REST}/depth",
            params={"symbol": symbol, "limit": 20},
            timeout=10
        )
        print(f"[Binance OB] {symbol}: status={resp.status_code}")
        data = resp.json()
        if "bids" not in data:
            print(f"[Binance OB] Error: {data.get('msg', data)}")
            return {"bids": [], "asks": [], "mid": 0}
        bids = [(float(p), float(q)) for p, q in data["bids"]]
        asks = [(float(p), float(q)) for p, q in data["asks"]]
        mid = (bids[0][0] + asks[0][0]) / 2 if bids and asks else 0
        return {"bids": bids, "asks": asks, "mid": mid}
    except Exception as e:
        print(f"[Binance OB] Exception: {e}")
        return {"bids": [], "asks": [], "mid": 0}


def fetch_binance_klines(symbol: str, interval: str, limit: int = 100) -> list:
    """Fetch klines/candles from Binance."""
    try:
        resp = http_session.get(
            f"{config.BINANCE_REST}/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10
        )
        data = resp.json()
        if not isinstance(data, list):
            print(f"[Binance Klines] Error: {data.get('msg', data)}")
            return []
        return [
            {
                "t": float(r[0]) / 1000,
                "o": float(r[1]),
                "h": float(r[2]),
                "l": float(r[3]),
                "c": float(r[4]),
                "v": float(r[5]),
            }
            for r in data
        ]
    except Exception as e:
        print(f"[Binance Klines] Exception: {e}")
        return []


# ── Polymarket Data Fetching ───────────────────────────────────────

def fetch_pm_market(coin: str, tf: str) -> dict:
    """Fetch Polymarket odds for a coin/timeframe."""
    from feeds import _build_slug

    slug = _build_slug(coin, tf)
    if not slug:
        return {"pm_up": None, "pm_dn": None}

    try:
        data = http_session.get(
            config.PM_GAMMA,
            params={"slug": slug, "limit": 1},
            timeout=5
        ).json()

        if not data or data[0].get("ticker") != slug:
            return {"pm_up": None, "pm_dn": None}

        market = data[0]["markets"][0]
        outcome_prices = market.get("outcomePrices", [])

        if outcome_prices and len(outcome_prices) >= 2:
            return {"pm_up": float(outcome_prices[0]), "pm_dn": float(outcome_prices[1])}

        return {"pm_up": None, "pm_dn": None}
    except Exception:
        return {"pm_up": None, "pm_dn": None}


# ── Indicator Calculations ─────────────────────────────────────────

def calculate_indicators(klines: list, bids: list, asks: list, mid: float) -> dict:
    """Calculate all indicators from raw data."""
    import indicators as ind

    if not klines or not mid:
        return {}

    obi_v = ind.obi(bids, asks, mid) if mid else 0.0
    bw, aw = ind.walls(bids, asks)
    rsi_v = ind.rsi(klines)
    macd_v, sig_v, hist_v = ind.macd(klines)
    vwap_v = ind.vwap(klines)
    ema_s, ema_l = ind.emas(klines)
    ha = ind.heikin_ashi(klines)

    return {
        "obi": obi_v,
        "obi_signal": "BULLISH" if obi_v > config.OBI_THRESH else "BEARISH" if obi_v < -config.OBI_THRESH else "NEUTRAL",
        "buy_walls": len(bw),
        "sell_walls": len(aw),
        "rsi": rsi_v,
        "rsi_signal": None if rsi_v is None else ("OVERBOUGHT" if rsi_v > config.RSI_OB else "OVERSOLD" if rsi_v < config.RSI_OS else None),
        "macd": macd_v,
        "macd_signal": sig_v,
        "macd_hist": hist_v,
        "macd_cross": None if hist_v is None else ("bullish" if hist_v > 0 else "bearish"),
        "vwap": vwap_v,
        "vwap_signal": None if not vwap_v or not mid else ("above" if mid > vwap_v else "below"),
        "ema_short": ema_s,
        "ema_long": ema_l,
        "ema_cross": None if ema_s is None or ema_l is None else ("golden" if ema_s > ema_l else "death"),
        "ha_last": [c["green"] for c in ha[-config.HA_COUNT:]] if ha else [],
    }


def calculate_trend_score(indicators: dict, mid: float) -> tuple[int, str]:
    """Calculate trend score from indicators."""
    score = 0

    obi_v = indicators.get("obi", 0)
    if obi_v > config.OBI_THRESH:
        score += 1
    elif obi_v < -config.OBI_THRESH:
        score -= 1

    rsi_v = indicators.get("rsi")
    if rsi_v is not None:
        if rsi_v > config.RSI_OB:
            score -= 1
        elif rsi_v < config.RSI_OS:
            score += 1

    hist_v = indicators.get("macd_hist")
    if hist_v is not None:
        score += 1 if hist_v > 0 else -1

    vwap_v = indicators.get("vwap")
    if vwap_v and mid:
        score += 1 if mid > vwap_v else -1

    ema_s = indicators.get("ema_short")
    ema_l = indicators.get("ema_long")
    if ema_s is not None and ema_l is not None:
        score += 1 if ema_s > ema_l else -1

    bw = indicators.get("buy_walls", 0)
    aw = indicators.get("sell_walls", 0)
    score += min(bw, 2)
    score -= min(aw, 2)

    ha = indicators.get("ha_last", [])
    if len(ha) >= 3:
        if all(ha[-3:]):
            score += 1
        elif not any(ha[-3:]):
            score -= 1

    if score >= 3:
        return score, "BULLISH"
    elif score <= -3:
        return score, "BEARISH"
    return score, "NEUTRAL"


# ── Data Fetching ──────────────────────────────────────────────────

def fetch_all_data() -> dict:
    """Fetch data for all coins (synchronous)."""
    cache_key = "all_coins"
    cached = get_cached(cache_key)
    if cached:
        return cached

    result = {}
    for coin in config.COINS:
        symbol = config.COIN_BINANCE[coin]
        ob = fetch_binance_orderbook(symbol)

        result[coin] = []
        for tf in config.TIMEFRAMES:
            kline_interval = config.TF_KLINE[tf]
            klines = fetch_binance_klines(symbol, kline_interval)
            pm = fetch_pm_market(coin, tf)
            indicators = calculate_indicators(klines, ob["bids"], ob["asks"], ob["mid"])
            score, trend = calculate_trend_score(indicators, ob["mid"])

            result[coin].append({
                "coin": coin,
                "timeframe": tf,
                "price": ob["mid"],
                "pm_up": pm.get("pm_up"),
                "pm_dn": pm.get("pm_dn"),
                "score": score,
                "trend": trend,
                "indicators": indicators,
                "timestamp": time.time(),
            })

    set_cached(cache_key, result)
    return result


# ── WebSocket Broadcast ────────────────────────────────────────────

async def broadcast_loop():
    """Broadcast updates to all connected WebSocket clients."""
    while True:
        await asyncio.sleep(config.REFRESH)
        if connected_clients:
            try:
                data = fetch_all_data()
                disconnected = []
                for client in connected_clients:
                    try:
                        await client.send_json(data)
                    except Exception:
                        disconnected.append(client)
                for client in disconnected:
                    if client in connected_clients:
                        connected_clients.remove(client)
            except Exception as e:
                print(f"Broadcast error: {e}")


# ── App Setup ──────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global broadcast_task
    print("[Server] Starting broadcast loop...")
    broadcast_task = asyncio.create_task(broadcast_loop())
    yield
    if broadcast_task:
        broadcast_task.cancel()


app = FastAPI(title="Polymarket Crypto Assistant", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Routes ─────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/coin/{coin}")
async def get_coin_data(coin: str):
    """Get all timeframe data for a single coin."""
    if coin not in config.COINS:
        return JSONResponse({"error": "Invalid coin"}, status_code=400)

    cache_key = f"coin_{coin}"
    cached = get_cached(cache_key)
    if cached:
        return cached

    symbol = config.COIN_BINANCE[coin]
    ob = fetch_binance_orderbook(symbol)

    results = []
    for tf in config.TIMEFRAMES:
        kline_interval = config.TF_KLINE[tf]
        klines = fetch_binance_klines(symbol, kline_interval)
        pm = fetch_pm_market(coin, tf)
        indicators = calculate_indicators(klines, ob["bids"], ob["asks"], ob["mid"])
        score, trend = calculate_trend_score(indicators, ob["mid"])

        results.append({
            "coin": coin,
            "timeframe": tf,
            "price": ob["mid"],
            "pm_up": pm.get("pm_up"),
            "pm_dn": pm.get("pm_dn"),
            "score": score,
            "trend": trend,
            "indicators": indicators,
            "timestamp": time.time(),
        })

    set_cached(cache_key, results)
    return results


@app.get("/api/all")
async def get_all_data():
    """Get data for all coins."""
    return fetch_all_data()


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": time.time()}


@app.get("/debug")
async def debug():
    """Debug endpoint to test API connections."""
    results = {}

    # Test Binance
    try:
        resp = http_session.get(
            f"{config.BINANCE_REST}/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=10
        )
        results["binance_status"] = resp.status_code
        results["binance_response"] = resp.text[:500]
    except Exception as e:
        results["binance_error"] = str(e)

    # Test Polymarket
    try:
        resp = http_session.get(
            config.PM_GAMMA,
            params={"slug": "btc-updown-15m-0", "limit": 1},
            timeout=10
        )
        results["pm_status"] = resp.status_code
        results["pm_response"] = resp.text[:500]
    except Exception as e:
        results["pm_error"] = str(e)

    return results


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    try:
        # Send initial data
        data = fetch_all_data()
        await websocket.send_json(data)
        # Keep connection alive
        while True:
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
    finally:
        if websocket in connected_clients:
            connected_clients.remove(websocket)
