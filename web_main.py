import sys
import os
import asyncio
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import uvicorn

import config
import feeds
from web.server import app, states, broadcast_loop


def get_market_expiry(tf: str) -> float:
    """Calculate seconds until current market expires."""
    now = time.time()

    if tf == "15m":
        # 15m markets align to 15-min boundaries
        period = 900
        next_boundary = ((int(now) // period) + 1) * period
        return max(next_boundary - now + 5, 10)  # +5s buffer

    elif tf == "1h":
        # 1h markets align to hour boundaries
        period = 3600
        next_boundary = ((int(now) // period) + 1) * period
        return max(next_boundary - now + 5, 10)

    elif tf == "4h":
        # 4h markets: offset by 1 hour, align to 4h boundaries
        period = 14400
        offset = 3600
        adjusted = now - offset
        next_boundary = ((int(adjusted) // period) + 1) * period + offset
        return max(next_boundary - now + 5, 10)

    elif tf == "daily":
        # Daily markets resolve at noon ET next day - refresh every hour
        return 3600

    return 300  # Default 5 min


async def start_coin_feeds(coin: str):
    binance_sym = config.COIN_BINANCE[coin]

    shared_ob_state = feeds.State()

    for tf in config.TIMEFRAMES:
        st = feeds.State()
        states[(coin, tf)] = st

        st.pm_up_id, st.pm_dn_id = feeds.fetch_pm_tokens(coin, tf)
        if st.pm_up_id:
            print(f"  [{coin}/{tf}] PM tokens loaded")
        else:
            print(f"  [{coin}/{tf}] No PM market available")

        kline_iv = config.TF_KLINE[tf]
        await feeds.bootstrap(binance_sym, kline_iv, st)

    print(f"  [{coin}] Starting feeds...")

    tasks = []

    async def sync_ob():
        while True:
            for tf in config.TIMEFRAMES:
                st = states[(coin, tf)]
                st.bids = shared_ob_state.bids
                st.asks = shared_ob_state.asks
                st.mid = shared_ob_state.mid
            await asyncio.sleep(0.5)

    tasks.append(asyncio.create_task(feeds.ob_poller(binance_sym, shared_ob_state)))
    tasks.append(asyncio.create_task(sync_ob()))

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
                        # Calculate time until market expires
                        timeout = get_market_expiry(timeframe)
                        print(f"  [PM] {coin_name}/{timeframe} market expires in {timeout:.0f}s")
                        try:
                            await asyncio.wait_for(feeds.pm_feed(state), timeout=timeout)
                        except asyncio.TimeoutError:
                            # Market expired, clear old prices and fetch new tokens
                            state.pm_up = None
                            state.pm_dn = None
                            print(f"  [PM] {coin_name}/{timeframe} market expired, refreshing...")
                            continue
                    else:
                        # No market available, retry in 30s
                        await asyncio.sleep(30)
                        continue
                except Exception as e:
                    print(f"  [PM] reconnecting ({coin_name}/{timeframe}): {e}")
                await asyncio.sleep(5)

        tasks.append(asyncio.create_task(binance_wrapper(binance_sym, kline_iv, st)))

        async def sync_trades(main_st, coin_key, tf_key):
            primary = states[(coin_key, "15m")]
            while True:
                if tf_key != "15m":
                    main_st.trades = primary.trades
                await asyncio.sleep(1)

        tasks.append(asyncio.create_task(sync_trades(st, coin, tf)))
        tasks.append(asyncio.create_task(pm_wrapper(st, coin, tf)))

    return tasks


async def main():
    print("\n[Polymarket Crypto Assistant - Web UI]\n")
    print(f"Starting server on http://localhost:{config.WEB_PORT}\n")

    all_tasks = []

    for coin in config.COINS:
        print(f"[{coin}] Initializing...")
        tasks = await start_coin_feeds(coin)
        all_tasks.extend(tasks)

    all_tasks.append(asyncio.create_task(broadcast_loop()))

    uvicorn_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=config.WEB_PORT,
        log_level="info",
    )
    server = uvicorn.Server(uvicorn_config)

    print(f"\n[Server] Starting on port {config.WEB_PORT}...")
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
