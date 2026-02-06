import asyncio
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# Add src directory to Python path for imports
_src_dir = Path(__file__).parent.parent
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

import config
import indicators as ind
import feeds
from feeds import State

STATIC_DIR = Path(__file__).parent / "static"

states: dict[tuple[str, str], State] = {}

connected_clients: list[WebSocket] = []

background_tasks: list[asyncio.Task] = []

TREND_THRESH = 3


def get_market_expiry(tf: str) -> float:
    now = time.time()
    if tf == "15m":
        period = 900
        next_boundary = ((int(now) // period) + 1) * period
        return max(next_boundary - now + 5, 10)
    elif tf == "1h":
        period = 3600
        next_boundary = ((int(now) // period) + 1) * period
        return max(next_boundary - now + 5, 10)
    elif tf == "4h":
        period = 14400
        offset = 3600
        adjusted = now - offset
        next_boundary = ((int(adjusted) // period) + 1) * period + offset
        return max(next_boundary - now + 5, 10)
    elif tf == "daily":
        return 3600
    return 300


async def start_coin_feeds(coin: str):
    binance_sym = config.COIN_BINANCE[coin]
    shared_ob_state = State()

    for tf in config.TIMEFRAMES:
        st = State()
        states[(coin, tf)] = st
        st.pm_up_id, st.pm_dn_id = feeds.fetch_pm_tokens(coin, tf)
        if st.pm_up_id:
            print(f"  [{coin}/{tf}] PM tokens loaded")
        else:
            print(f"  [{coin}/{tf}] No PM market available")
        kline_iv = config.TF_KLINE[tf]
        await feeds.bootstrap(binance_sym, kline_iv, st)

    print(f"  [{coin}] Starting feeds...")

    async def sync_ob():
        while True:
            for tf in config.TIMEFRAMES:
                st = states[(coin, tf)]
                st.bids = shared_ob_state.bids
                st.asks = shared_ob_state.asks
                st.mid = shared_ob_state.mid
            await asyncio.sleep(0.5)

    background_tasks.append(asyncio.create_task(feeds.ob_poller(binance_sym, shared_ob_state)))
    background_tasks.append(asyncio.create_task(sync_ob()))

    for tf in config.TIMEFRAMES:
        st = states[(coin, tf)]
        kline_iv = config.TF_KLINE[tf]

        async def binance_wrapper(sym, kiv, state):
            while True:
                try:
                    await feeds.binance_feed(sym, kiv, state)
                except Exception as e:
                    print(f"  [Binance] reconnecting ({sym} {kiv}): {e}")
                    await asyncio.sleep(2)

        async def pm_wrapper(state, coin_name, timeframe):
            while True:
                try:
                    state.pm_up_id, state.pm_dn_id = feeds.fetch_pm_tokens(coin_name, timeframe)
                    if state.pm_up_id:
                        timeout = get_market_expiry(timeframe)
                        print(f"  [PM] {coin_name}/{timeframe} market expires in {timeout:.0f}s")
                        try:
                            await asyncio.wait_for(feeds.pm_feed(state), timeout=timeout)
                        except asyncio.TimeoutError:
                            state.pm_up = None
                            state.pm_dn = None
                            print(f"  [PM] {coin_name}/{timeframe} market expired, refreshing...")
                            continue
                    else:
                        await asyncio.sleep(30)
                        continue
                except Exception as e:
                    print(f"  [PM] reconnecting ({coin_name}/{timeframe}): {e}")
                await asyncio.sleep(5)

        async def sync_trades(main_st, coin_key, tf_key):
            primary = states[(coin_key, "15m")]
            while True:
                if tf_key != "15m":
                    main_st.trades = primary.trades
                await asyncio.sleep(1)

        background_tasks.append(asyncio.create_task(binance_wrapper(binance_sym, kline_iv, st)))
        background_tasks.append(asyncio.create_task(sync_trades(st, coin, tf)))
        background_tasks.append(asyncio.create_task(pm_wrapper(st, coin, tf)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n[Polymarket Crypto Assistant - Web UI]\n")
    for coin in config.COINS:
        print(f"[{coin}] Initializing...")
        await start_coin_feeds(coin)
    background_tasks.append(asyncio.create_task(broadcast_loop()))
    print("\n[Server] Ready!\n")
    yield
    for task in background_tasks:
        task.cancel()


app = FastAPI(title="Polymarket Crypto Assistant", lifespan=lifespan)


def calculate_trend_score(st: State) -> tuple[int, str]:
    score = 0

    obi_v = ind.obi(st.bids, st.asks, st.mid) if st.mid else 0.0
    if obi_v > config.OBI_THRESH:
        score += 1
    elif obi_v < -config.OBI_THRESH:
        score -= 1

    cvd5 = ind.cvd(st.trades, 300)
    score += 1 if cvd5 > 0 else -1 if cvd5 < 0 else 0

    rsi_v = ind.rsi(st.klines)
    if rsi_v is not None:
        if rsi_v > config.RSI_OB:
            score -= 1
        elif rsi_v < config.RSI_OS:
            score += 1

    _, _, hv = ind.macd(st.klines)
    if hv is not None:
        score += 1 if hv > 0 else -1

    vwap_v = ind.vwap(st.klines)
    if vwap_v and st.mid:
        score += 1 if st.mid > vwap_v else -1

    es, el = ind.emas(st.klines)
    if es is not None and el is not None:
        score += 1 if es > el else -1

    bw, aw = ind.walls(st.bids, st.asks)
    score += min(len(bw), 2)
    score -= min(len(aw), 2)

    ha = ind.heikin_ashi(st.klines)
    if len(ha) >= 3:
        last3 = ha[-3:]
        if all(c["green"] for c in last3):
            score += 1
        elif all(not c["green"] for c in last3):
            score -= 1

    if score >= TREND_THRESH:
        return score, "BULLISH"
    elif score <= -TREND_THRESH:
        return score, "BEARISH"
    else:
        return score, "NEUTRAL"


def serialize_state(coin: str, tf: str, st: State) -> dict[str, Any]:
    obi_v = ind.obi(st.bids, st.asks, st.mid) if st.mid else 0.0
    bw, aw = ind.walls(st.bids, st.asks)
    dep = ind.depth_usd(st.bids, st.asks, st.mid) if st.mid else {}

    cvd_values = {s: ind.cvd(st.trades, s) for s in config.CVD_WINDOWS}

    rsi_v = ind.rsi(st.klines)
    macd_v, sig_v, hist_v = ind.macd(st.klines)
    vwap_v = ind.vwap(st.klines)
    ema_s, ema_l = ind.emas(st.klines)
    ha = ind.heikin_ashi(st.klines)

    score, trend = calculate_trend_score(st)

    return {
        "coin": coin,
        "timeframe": tf,
        "price": st.mid,
        "pm_up": st.pm_up,
        "pm_dn": st.pm_dn,
        "score": score,
        "trend": trend,
        "indicators": {
            "obi": obi_v,
            "obi_signal": "BULLISH" if obi_v > config.OBI_THRESH else "BEARISH" if obi_v < -config.OBI_THRESH else "NEUTRAL",
            "buy_walls": len(bw),
            "sell_walls": len(aw),
            "buy_wall_prices": [p for p, _ in bw[:3]],
            "sell_wall_prices": [p for p, _ in aw[:3]],
            "depth": dep,
            "cvd_1m": cvd_values.get(60, 0),
            "cvd_3m": cvd_values.get(180, 0),
            "cvd_5m": cvd_values.get(300, 0),
            "rsi": rsi_v,
            "rsi_signal": None if rsi_v is None else ("OVERBOUGHT" if rsi_v > config.RSI_OB else "OVERSOLD" if rsi_v < config.RSI_OS else None),
            "macd": macd_v,
            "macd_signal": sig_v,
            "macd_hist": hist_v,
            "macd_cross": None if hist_v is None else ("bullish" if hist_v > 0 else "bearish"),
            "vwap": vwap_v,
            "vwap_signal": None if not vwap_v or not st.mid else ("above" if st.mid > vwap_v else "below"),
            "ema_short": ema_s,
            "ema_long": ema_l,
            "ema_cross": None if ema_s is None or ema_l is None else ("golden" if ema_s > ema_l else "death"),
            "ha_last": [c["green"] for c in ha[-config.HA_COUNT:]] if ha else [],
        },
        "timestamp": time.time(),
    }


def get_all_states() -> dict[str, list[dict]]:
    result = {}
    for coin in config.COINS:
        result[coin] = []
        for tf in config.TIMEFRAMES:
            key = (coin, tf)
            if key in states:
                st = states[key]
                if st.mid > 0 and st.klines:
                    result[coin].append(serialize_state(coin, tf, st))
                else:
                    result[coin].append({
                        "coin": coin,
                        "timeframe": tf,
                        "loading": True,
                        "timestamp": time.time(),
                    })
            else:
                result[coin].append({
                    "coin": coin,
                    "timeframe": tf,
                    "loading": True,
                    "timestamp": time.time(),
                })
    return result


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/snapshot")
async def snapshot():
    return get_all_states()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    try:
        await websocket.send_json(get_all_states())
        while True:
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
    finally:
        if websocket in connected_clients:
            connected_clients.remove(websocket)


async def broadcast_loop():
    while True:
        await asyncio.sleep(config.REFRESH)
        if connected_clients:
            data = get_all_states()
            disconnected = []
            for client in connected_clients:
                try:
                    await client.send_json(data)
                except Exception:
                    disconnected.append(client)
            for client in disconnected:
                if client in connected_clients:
                    connected_clients.remove(client)
