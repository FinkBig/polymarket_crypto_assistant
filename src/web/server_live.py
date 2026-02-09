"""
Live WebSocket-based server for local development.
Uses real-time WebSocket feeds from Binance and Polymarket.
"""
import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

# Add src directory to Python path
_src_dir = Path(__file__).parent.parent
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

import config
import database as db
import indicators as ind
from feeds import State, ob_poller, binance_feed, bootstrap, fetch_pm_tokens, pm_feed, _build_slug
import requests

STATIC_DIR = Path(__file__).parent / "static"

# State storage: {(coin, tf): State}
states: dict[tuple[str, str], State] = {}

# Market metadata: {(coin, tf): {"strike": float, "expiry": float, "start": float}}
market_meta: dict[tuple[str, str], dict] = {}

# WebSocket clients
connected_clients: list[WebSocket] = []

# Track last broadcast time
last_broadcast = 0
BROADCAST_INTERVAL = 1  # Push updates every 1 second

# ── Trend & recommendation stability ─────────────────────────────
# Hysteresis: enter at TREND_ENTER, exit only when abs(score) drops below TREND_EXIT
TREND_ENTER = 3   # score must reach ±3 to trigger BULLISH/BEARISH
TREND_EXIT  = 1   # score must drop to ±1 to revert to NEUTRAL

# Minimum hold: once a recommendation fires, keep it for at least this many seconds
REC_HOLD_SECONDS = 45

# Persistent state for hysteresis and hold timers
# {(coin, tf): "BULLISH" | "BEARISH" | "NEUTRAL"}
_prev_trend: dict[tuple[str, str], str] = {}
# {(coin, tf): {"rec": {...}, "expires": float}}
_held_recs: dict[tuple[str, str], dict] = {}

# Track which signals have been logged: {(coin, tf): set of (market_id, action)}
_logged_signals: dict[tuple[str, str], set] = {}


def _market_id(coin: str, tf: str, meta: dict) -> str:
    """Build a unique market_id like BTC-15m-1770570000."""
    start = int(meta.get("start", 0))
    return f"{coin}-{tf}-{start}"


def calculate_indicators(state: State) -> dict:
    """Calculate all indicators from state."""
    klines = state.klines + ([state.cur_kline] if state.cur_kline else [])

    if not klines or not state.mid:
        return {}

    obi_v = ind.obi(state.bids, state.asks, state.mid) if state.mid else 0.0
    bw, aw = ind.walls(state.bids, state.asks)
    rsi_v = ind.rsi(klines)
    macd_v, sig_v, hist_v = ind.macd(klines)
    vwap_v = ind.vwap(klines)
    ema_s, ema_l = ind.emas(klines)
    ha = ind.heikin_ashi(klines)
    cvd_5m = ind.cvd(state.trades, 300)

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
        "vwap_signal": None if not vwap_v or not state.mid else ("above" if state.mid > vwap_v else "below"),
        "ema_short": ema_s,
        "ema_long": ema_l,
        "ema_cross": None if ema_s is None or ema_l is None else ("golden" if ema_s > ema_l else "death"),
        "ha_last": [c["green"] for c in ha[-config.HA_COUNT:]] if ha else [],
        "cvd_5m": cvd_5m,
    }


def _raw_score(indicators: dict, mid: float) -> int:
    """Calculate raw trend score from indicators (no hysteresis)."""
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

    return score


def calculate_trend_score(indicators: dict, mid: float, coin: str = "", tf: str = "") -> tuple[int, str]:
    """Calculate trend score with hysteresis to prevent flickering."""
    score = _raw_score(indicators, mid)
    key = (coin, tf)
    prev = _prev_trend.get(key, "NEUTRAL")

    # Hysteresis: require TREND_ENTER to switch into a trend,
    # but only revert to NEUTRAL when score drops to TREND_EXIT
    if prev == "NEUTRAL":
        if score >= TREND_ENTER:
            trend = "BULLISH"
        elif score <= -TREND_ENTER:
            trend = "BEARISH"
        else:
            trend = "NEUTRAL"
    elif prev == "BULLISH":
        if score <= -TREND_ENTER:
            trend = "BEARISH"
        elif score >= TREND_EXIT:
            trend = "BULLISH"  # hold bullish until score drops below exit
        else:
            trend = "NEUTRAL"
    else:  # prev == "BEARISH"
        if score >= TREND_ENTER:
            trend = "BULLISH"
        elif score <= -TREND_EXIT:
            trend = "BEARISH"  # hold bearish until score rises above exit
        else:
            trend = "NEUTRAL"

    _prev_trend[key] = trend
    return score, trend


def calculate_recommendation(
    score: int, trend: str, indicators: dict,
    pm_up: float | None, pm_dn: float | None,
    current_price: float | None, strike: float | None,
    time_remaining: float | None
) -> dict:
    """
    Calculate trading recommendation based on technical analysis, Polymarket odds,
    strike price, and time remaining.

    Returns dict with:
    - action: "BUY_YES", "BUY_NO", or "WAIT"
    - confidence: "HIGH", "MEDIUM", or None
    - reason: Brief explanation
    """
    # No recommendation if Polymarket data is missing
    if pm_up is None or pm_dn is None:
        return {"action": "WAIT", "confidence": None, "reason": "No PM data"}

    # Check time remaining - don't trade if less than 2 minutes
    if time_remaining is not None and time_remaining < 120:
        return {"action": "WAIT", "confidence": None, "reason": f"Only {int(time_remaining)}s left"}

    # Check price vs strike with time factor
    if strike is not None and current_price is not None and time_remaining is not None:
        price_diff_pct = (current_price - strike) / strike * 100

        # If we're significantly below strike with little time, avoid BUY_YES
        if time_remaining < 300 and price_diff_pct < -0.3:  # < 5 min and 0.3% below
            if trend == "BULLISH":
                return {"action": "WAIT", "confidence": None, "reason": f"Below strike (-{abs(price_diff_pct):.2f}%) with {int(time_remaining)}s left"}

        # If we're significantly above strike with little time, avoid BUY_NO
        if time_remaining < 300 and price_diff_pct > 0.3:  # < 5 min and 0.3% above
            if trend == "BEARISH":
                return {"action": "WAIT", "confidence": None, "reason": f"Above strike (+{price_diff_pct:.2f}%) with {int(time_remaining)}s left"}

    rsi = indicators.get("rsi")
    macd_cross = indicators.get("macd_cross")
    ema_cross = indicators.get("ema_cross")
    ha_last = indicators.get("ha_last", [])

    # Count confirming signals for confidence
    bullish_confirms = 0
    bearish_confirms = 0

    if rsi is not None:
        if rsi < 30:  # Oversold
            bullish_confirms += 1
        elif rsi > 70:  # Overbought
            bearish_confirms += 1

    if macd_cross == "bullish":
        bullish_confirms += 1
    elif macd_cross == "bearish":
        bearish_confirms += 1

    if ema_cross == "golden":
        bullish_confirms += 1
    elif ema_cross == "death":
        bearish_confirms += 1

    if len(ha_last) >= 3:
        if all(ha_last[-3:]):  # 3 green HA candles
            bullish_confirms += 1
        elif not any(ha_last[-3:]):  # 3 red HA candles
            bearish_confirms += 1

    # ── BUY YES Logic ──
    # Strong bullish technicals + underpriced YES odds
    if trend == "BULLISH" and pm_up < 0.55:
        # High confidence: strong score + multiple confirms + very cheap odds
        if score >= 4 and bullish_confirms >= 2 and pm_up < 0.45:
            return {
                "action": "BUY_YES",
                "confidence": "HIGH",
                "reason": f"Strong bullish ({score}) + YES at {pm_up:.0%}"
            }
        # Medium confidence: decent score + some edge
        elif score >= 3 and pm_up < 0.50:
            return {
                "action": "BUY_YES",
                "confidence": "MEDIUM",
                "reason": f"Bullish ({score}) + YES at {pm_up:.0%}"
            }

    # ── BUY NO Logic ──
    # Strong bearish technicals + underpriced NO odds
    if trend == "BEARISH" and pm_dn < 0.55:
        # High confidence: strong score + multiple confirms + very cheap odds
        if score <= -4 and bearish_confirms >= 2 and pm_dn < 0.45:
            return {
                "action": "BUY_NO",
                "confidence": "HIGH",
                "reason": f"Strong bearish ({score}) + NO at {pm_dn:.0%}"
            }
        # Medium confidence: decent score + some edge
        elif score <= -3 and pm_dn < 0.50:
            return {
                "action": "BUY_NO",
                "confidence": "MEDIUM",
                "reason": f"Bearish ({score}) + NO at {pm_dn:.0%}"
            }

    # ── Special cases: RSI extremes with cheap odds ──
    if rsi is not None:
        # Extremely oversold + cheap YES
        if rsi < 25 and pm_up < 0.45 and score > 0:
            return {
                "action": "BUY_YES",
                "confidence": "MEDIUM",
                "reason": f"RSI oversold ({rsi:.0f}) + YES at {pm_up:.0%}"
            }
        # Extremely overbought + cheap NO
        if rsi > 75 and pm_dn < 0.45 and score < 0:
            return {
                "action": "BUY_NO",
                "confidence": "MEDIUM",
                "reason": f"RSI overbought ({rsi:.0f}) + NO at {pm_dn:.0%}"
            }

    # ── No clear signal ──
    reason = "No clear edge"
    if trend == "NEUTRAL":
        reason = "Neutral trend"
    elif trend == "BULLISH" and pm_up >= 0.55:
        reason = f"Bullish but YES expensive ({pm_up:.0%})"
    elif trend == "BEARISH" and pm_dn >= 0.55:
        reason = f"Bearish but NO expensive ({pm_dn:.0%})"

    return {"action": "WAIT", "confidence": None, "reason": reason}


def serialize_all_data() -> dict:
    """Serialize all states for WebSocket broadcast."""
    result = {}
    now = time.time()

    for coin in config.COINS:
        result[coin] = []
        for tf in config.TIMEFRAMES:
            state = states.get((coin, tf))
            if not state:
                continue

            # Get market metadata
            meta = market_meta.get((coin, tf), {})
            strike = meta.get("strike")
            expiry = meta.get("expiry")
            time_remaining = (expiry - now) if expiry else None
            market_url = meta.get("url")

            indicators = calculate_indicators(state)
            score, trend = calculate_trend_score(indicators, state.mid, coin, tf)
            new_rec = calculate_recommendation(
                score, trend, indicators,
                state.pm_up, state.pm_dn,
                state.mid, strike, time_remaining
            )

            # Minimum hold: keep an active recommendation for REC_HOLD_SECONDS
            key = (coin, tf)
            held = _held_recs.get(key)
            if new_rec["action"] != "WAIT":
                # New actionable recommendation — (re)start the hold timer
                if not held or held["rec"]["action"] != new_rec["action"] or now >= held["expires"]:
                    _held_recs[key] = {"rec": new_rec, "expires": now + REC_HOLD_SECONDS}
                else:
                    # Same action still active — extend the expiry
                    _held_recs[key]["expires"] = now + REC_HOLD_SECONDS
                    _held_recs[key]["rec"] = new_rec
                recommendation = new_rec
            elif held and now < held["expires"]:
                # Current calc says WAIT but we're still in the hold window
                recommendation = held["rec"]
            else:
                # No hold active, use WAIT
                _held_recs.pop(key, None)
                recommendation = new_rec

            # ── Signal logging ──────────────────────────────────
            meta = market_meta.get((coin, tf), {})
            entry_price = state.pm_up if recommendation.get("action") == "BUY_YES" else state.pm_dn
            if (recommendation["action"] in ("BUY_YES", "BUY_NO")
                    and meta.get("start")
                    and entry_price is not None and entry_price >= 0.05):
                mid = _market_id(coin, tf, meta)
                log_key = (mid, recommendation["action"])
                seen = _logged_signals.setdefault(key, set())
                if log_key not in seen:
                    seen.add(log_key)
                    # Build coroutine eagerly to avoid closure-in-loop bug
                    coro = db.async_log_signal(
                        market_id=mid, coin=coin, timeframe=tf,
                        action=recommendation["action"],
                        confidence=recommendation.get("confidence"),
                        reason=recommendation.get("reason"),
                        price=state.pm_up if recommendation["action"] == "BUY_YES" else state.pm_dn,
                        strike_price=strike, score=score, trend=trend,
                        pm_up=state.pm_up, pm_dn=state.pm_dn,
                        time_remaining=time_remaining,
                        rsi=indicators.get("rsi"),
                        macd_hist=indicators.get("macd_hist"),
                        obi=indicators.get("obi"),
                    )
                    asyncio.get_event_loop().call_soon_threadsafe(
                        lambda c=coro: asyncio.ensure_future(c)
                    )

            result[coin].append({
                "coin": coin,
                "timeframe": tf,
                "price": state.mid,
                "strike": strike,
                "time_remaining": time_remaining,
                "market_url": market_url,
                "pm_up": state.pm_up,
                "pm_dn": state.pm_dn,
                "score": score,
                "trend": trend,
                "recommendation": recommendation,
                "indicators": indicators,
                "timestamp": now,
            })
    return result


async def broadcast_loop():
    """Broadcast updates to all connected WebSocket clients."""
    global last_broadcast
    while True:
        await asyncio.sleep(BROADCAST_INTERVAL)
        if connected_clients:
            try:
                data = serialize_all_data()
                disconnected = []
                for client in connected_clients:
                    try:
                        await client.send_json(data)
                    except Exception:
                        disconnected.append(client)
                for client in disconnected:
                    if client in connected_clients:
                        connected_clients.remove(client)
                last_broadcast = time.time()
            except Exception as e:
                print(f"Broadcast error: {e}")


async def start_feeds():
    """Initialize all states and start feed coroutines."""
    tasks = []

    for coin in config.COINS:
        symbol = config.COIN_BINANCE[coin]

        # Create shared orderbook state (one per coin)
        ob_state = State()

        for tf in config.TIMEFRAMES:
            state = State()
            states[(coin, tf)] = state

            # Bootstrap historical klines
            kline_interval = config.TF_KLINE[tf]
            print(f"[{coin}/{tf}] Bootstrapping...", flush=True)
            try:
                bootstrap_sync(symbol, kline_interval, state)
            except Exception as e:
                print(f"[{coin}/{tf}] Bootstrap error: {e}", flush=True)

            # Start Polymarket WebSocket feed (handles token refresh on expiry)
            tasks.append(asyncio.create_task(pm_feed_wrapper(state, coin, tf)))

        # Start orderbook poller (shared per coin)
        tasks.append(asyncio.create_task(ob_poller_wrapper(symbol, coin)))

        # Start Binance WebSocket feeds (one per coin/timeframe)
        for tf in config.TIMEFRAMES:
            state = states[(coin, tf)]
            kline_interval = config.TF_KLINE[tf]
            tasks.append(asyncio.create_task(binance_feed_wrapper(symbol, kline_interval, state, coin, tf)))

    # Start broadcast loop
    tasks.append(asyncio.create_task(broadcast_loop()))

    print(f"\n[Server] All feeds started! Listening on http://localhost:8080\n")

    return tasks


def bootstrap_sync(symbol: str, interval: str, state: State):
    """Synchronous bootstrap wrapper."""
    import requests
    resp = requests.get(
        f"{config.BINANCE_REST}/klines",
        params={"symbol": symbol, "interval": interval, "limit": config.KLINE_BOOT},
        timeout=10
    ).json()
    if isinstance(resp, list):
        state.klines = [
            {
                "t": r[0] / 1e3,
                "o": float(r[1]), "h": float(r[2]),
                "l": float(r[3]), "c": float(r[4]),
                "v": float(r[5]),
            }
            for r in resp
        ]
        print(f"  [Binance] loaded {len(state.klines)} candles")


def _fetch_ob(symbol: str):
    """Fetch orderbook synchronously (runs in executor)."""
    resp = requests.get(
        f"{config.BINANCE_REST}/depth",
        params={"symbol": symbol, "limit": 20},
        timeout=3
    ).json()
    bids = [(float(p), float(q)) for p, q in resp.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in resp.get("asks", [])]
    mid = (bids[0][0] + asks[0][0]) / 2 if bids and asks else 0
    return bids, asks, mid


async def ob_poller_wrapper(symbol: str, coin: str):
    """Wrapper for orderbook poller that updates all timeframe states."""
    loop = asyncio.get_running_loop()
    print(f"[{coin}] Starting orderbook poller")
    while True:
        try:
            bids, asks, mid = await loop.run_in_executor(None, _fetch_ob, symbol)

            # Update all timeframe states for this coin
            for tf in config.TIMEFRAMES:
                state = states.get((coin, tf))
                if state:
                    state.bids = bids
                    state.asks = asks
                    state.mid = mid
        except Exception as e:
            print(f"[{coin}] OB error: {e}")
        await asyncio.sleep(2)


async def binance_feed_wrapper(symbol: str, kline_interval: str, state: State, coin: str, tf: str):
    """Wrapper for Binance WebSocket feed with reconnection."""
    import websockets

    while True:
        try:
            sym = symbol.lower()
            streams = "/".join([
                f"{sym}@trade",
                f"{sym}@kline_{kline_interval}",
            ])
            url = f"{config.BINANCE_WS}?streams={streams}"

            async with websockets.connect(url, ping_interval=20) as ws:
                print(f"[{coin}/{tf}] Binance WS connected")
                while True:
                    data = json.loads(await ws.recv())
                    stream = data.get("stream", "")
                    pay = data["data"]

                    if "@trade" in stream:
                        state.trades.append({
                            "t": pay["T"] / 1000.0,
                            "price": float(pay["p"]),
                            "qty": float(pay["q"]),
                            "is_buy": not pay["m"],
                        })
                        if len(state.trades) > 5000:
                            cut = time.time() - config.TRADE_TTL
                            state.trades = [t for t in state.trades if t["t"] >= cut]

                    elif "@kline" in stream:
                        k = pay["k"]
                        candle = {
                            "t": k["t"] / 1000.0,
                            "o": float(k["o"]), "h": float(k["h"]),
                            "l": float(k["l"]), "c": float(k["c"]),
                            "v": float(k["v"]),
                        }
                        state.cur_kline = candle
                        if k["x"]:  # Kline closed
                            state.klines.append(candle)
                            state.klines = state.klines[-config.KLINE_MAX:]

        except Exception as e:
            print(f"[{coin}/{tf}] Binance WS error: {e}, reconnecting...")
            await asyncio.sleep(5)


def fetch_market_metadata(coin: str, tf: str) -> dict | None:
    """Fetch market metadata including strike price from Polymarket API."""
    slug = _build_slug(coin, tf)
    if not slug:
        return None

    try:
        resp = requests.get(
            config.PM_GAMMA,
            params={"slug": slug, "limit": 1},
            timeout=5
        ).json()

        if not resp or resp[0].get("ticker") != slug:
            return None

        now = time.time()

        # Calculate period start and expiry based on timeframe
        from datetime import datetime, timezone, timedelta

        if tf == "15m":
            period_start = (now // 900) * 900
            expiry = period_start + 900
        elif tf == "1h":
            period_start = (now // 3600) * 3600
            expiry = period_start + 3600
        elif tf == "4h":
            # 4h markets are offset by 1 hour
            period_start = ((now - 3600) // 14400) * 14400 + 3600
            expiry = period_start + 14400
        else:  # daily
            # Daily market: strike is 12:00 PM ET yesterday, expiry is 12:00 PM ET today
            # ET offset: -5 hours (EST) or -4 hours (EDT)
            utc_now = datetime.now(timezone.utc)

            # Determine if DST (rough check: March-November)
            is_dst = 3 <= utc_now.month <= 10
            et_offset = timedelta(hours=-4 if is_dst else -5)

            # 12:00 PM ET = 17:00 UTC (EST) or 16:00 UTC (EDT)
            noon_utc_hour = 16 if is_dst else 17

            # Today's noon ET in UTC
            today_noon_et = utc_now.replace(hour=noon_utc_hour, minute=0, second=0, microsecond=0)

            # If we're past noon ET, expiry is today's noon; strike is yesterday's noon
            # If we're before noon ET, expiry is today's noon; strike is yesterday's noon
            expiry = today_noon_et.timestamp()
            if now >= expiry:
                # Market expired, next one
                expiry = (today_noon_et + timedelta(days=1)).timestamp()
                period_start = today_noon_et.timestamp()
            else:
                period_start = (today_noon_et - timedelta(days=1)).timestamp()

        # Fetch strike price from Binance
        strike = None
        symbol = config.COIN_BINANCE[coin]
        try:
            if tf == "daily":
                # For daily: get the 1-minute candle CLOSE price at period_start (12:00 PM ET)
                kline_resp = requests.get(
                    f"{config.BINANCE_REST}/klines",
                    params={
                        "symbol": symbol,
                        "interval": "1m",
                        "startTime": int(period_start * 1000),
                        "limit": 1
                    },
                    timeout=5
                ).json()
                if kline_resp and isinstance(kline_resp, list) and len(kline_resp) > 0:
                    strike = float(kline_resp[0][4])  # Close price for daily
            else:
                # For other timeframes: get the candle OPEN price
                kline_resp = requests.get(
                    f"{config.BINANCE_REST}/klines",
                    params={
                        "symbol": symbol,
                        "interval": config.TF_KLINE[tf],
                        "startTime": int(period_start * 1000),
                        "limit": 1
                    },
                    timeout=5
                ).json()
                if kline_resp and isinstance(kline_resp, list) and len(kline_resp) > 0:
                    strike = float(kline_resp[0][1])  # Open price
        except Exception as e:
            print(f"[{coin}/{tf}] Failed to fetch strike price: {e}")

        return {
            "strike": strike,
            "expiry": expiry,
            "start": period_start,
            "slug": slug,
            "url": f"https://polymarket.com/event/{slug}"
        }
    except Exception as e:
        print(f"[{coin}/{tf}] Failed to fetch market metadata: {e}")
        return None


def get_market_expiry(tf: str) -> float:
    """Calculate when the current market expires (Unix timestamp)."""
    now = time.time()

    if tf == "15m":
        # Markets expire at :00, :15, :30, :45
        interval = 900  # 15 minutes in seconds
        return ((now // interval) + 1) * interval

    elif tf == "1h":
        # Markets expire at the top of each hour
        interval = 3600
        return ((now // interval) + 1) * interval

    elif tf == "4h":
        # Markets expire every 4 hours, offset by 1 hour (1:00, 5:00, 9:00, etc.)
        interval = 14400  # 4 hours
        offset = 3600  # 1 hour offset
        return (((now - offset) // interval) + 1) * interval + offset

    elif tf == "daily":
        # Daily markets - expire at midnight ET (roughly)
        # For simplicity, refresh every hour to catch the transition
        return now + 3600

    return now + 900  # Default: 15 minutes


async def pm_feed_wrapper(state: State, coin: str, tf: str):
    """Wrapper for Polymarket WebSocket feed with reconnection and market refresh."""
    import websockets

    loop = asyncio.get_running_loop()
    while True:
        try:
            # Fetch fresh token IDs for current market (sync → executor)
            up_id, dn_id = await loop.run_in_executor(None, fetch_pm_tokens, coin, tf)
            if not up_id:
                print(f"[{coin}/{tf}] No PM market available, retrying in 60s...")
                await asyncio.sleep(60)
                continue

            state.pm_up_id = up_id
            state.pm_dn_id = dn_id

            # Fetch market metadata (strike price, expiry) (sync → executor)
            meta = await loop.run_in_executor(None, fetch_market_metadata, coin, tf)
            if meta:
                market_meta[(coin, tf)] = meta
                print(f"[{coin}/{tf}] Strike: ${meta['strike']:.2f}" if meta['strike'] else f"[{coin}/{tf}] No strike price")

            # Calculate when this market expires
            expiry = meta["expiry"] if meta else get_market_expiry(tf)
            time_until_expiry = expiry - time.time()
            print(f"[{coin}/{tf}] PM market expires in {time_until_expiry:.0f}s")

            assets = [state.pm_up_id, state.pm_dn_id]
            async with websockets.connect(config.PM_WS, ping_interval=20) as ws:
                await ws.send(json.dumps({"assets_ids": assets, "type": "market"}))
                print(f"[{coin}/{tf}] PM WS connected")

                while True:
                    # Check if market has expired
                    if time.time() >= expiry:
                        print(f"[{coin}/{tf}] PM market expired, refreshing...")
                        # Log outcome
                        if meta and state.mid and meta.get("start"):
                            mid = _market_id(coin, tf, meta)
                            asyncio.create_task(db.async_log_outcome(
                                market_id=mid, coin=coin, timeframe=tf,
                                strike_price=meta.get("strike"),
                                final_price=state.mid,
                            ))
                            # Clear logged signals for this market
                            _logged_signals.pop((coin, tf), None)
                        break  # Exit inner loop to refresh tokens

                    # Use wait_for with timeout to periodically check expiry
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        raw = json.loads(raw)

                        if isinstance(raw, list):
                            for entry in raw:
                                _pm_apply(entry.get("asset_id"), entry.get("asks", []), state)

                        elif isinstance(raw, dict) and raw.get("event_type") == "price_change":
                            for ch in raw.get("price_changes", []):
                                if ch.get("best_ask"):
                                    _pm_set(ch["asset_id"], float(ch["best_ask"]), state)

                    except asyncio.TimeoutError:
                        # Timeout is fine, just check expiry and continue
                        pass

        except Exception as e:
            print(f"[{coin}/{tf}] PM WS error: {e}, reconnecting in 5s...")
            await asyncio.sleep(5)


def _pm_apply(asset, asks, state):
    if asks:
        _pm_set(asset, min(float(a["price"]) for a in asks), state)


def _pm_set(asset, price, state):
    if asset == state.pm_up_id:
        state.pm_up = price
    elif asset == state.pm_dn_id:
        state.pm_dn = price


# Store tasks globally so they don't get garbage collected
_feed_tasks = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _feed_tasks
    print("\n[Server] Initializing database...")
    db.init_db()
    print("[Server] Starting live WebSocket feeds...\n")
    _feed_tasks = await start_feeds()
    yield
    print("\n[Server] Shutting down...")
    for task in _feed_tasks:
        task.cancel()


app = FastAPI(title="Polymarket Crypto Assistant (Live)", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/all")
async def get_all_data():
    """Get current data for all coins."""
    return serialize_all_data()


@app.get("/health")
async def health():
    return {"status": "ok", "mode": "live", "timestamp": time.time()}


@app.get("/api/stats")
async def api_stats(
    coin: str | None = Query(None),
    timeframe: str | None = Query(None),
    confidence: str | None = Query(None),
):
    """Aggregated signal performance stats."""
    return await db.async_get_stats(coin=coin, timeframe=timeframe, confidence=confidence)


@app.get("/api/backtest")
async def api_backtest(
    coin: str | None = Query(None),
    timeframe: str | None = Query(None),
    confidence: str | None = Query(None),
):
    """Backtest data: chronological trades + aggregate stats."""
    return await db.async_get_backtest(coin=coin, timeframe=timeframe, confidence=confidence)


@app.get("/api/signals")
async def api_signals(
    coin: str | None = Query(None),
    timeframe: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    """Recent signals with outcomes."""
    return await db.async_get_signals(coin=coin, timeframe=timeframe, limit=limit)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    try:
        # Send initial data
        data = serialize_all_data()
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
