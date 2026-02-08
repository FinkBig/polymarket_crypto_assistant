"""
SQLite database for logging trading signals and market outcomes.
"""
import asyncio
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "signals.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables and views if they don't exist."""
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id   TEXT NOT NULL,
            coin        TEXT NOT NULL,
            timeframe   TEXT NOT NULL,
            action      TEXT NOT NULL,
            confidence  TEXT,
            reason      TEXT,
            price       REAL,
            strike_price REAL,
            score       INTEGER,
            trend       TEXT,
            pm_up       REAL,
            pm_dn       REAL,
            time_remaining REAL,
            rsi         REAL,
            macd_hist   REAL,
            obi         REAL,
            created_at  REAL NOT NULL,
            UNIQUE(market_id, action)
        );

        CREATE TABLE IF NOT EXISTS market_outcomes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id   TEXT UNIQUE NOT NULL,
            coin        TEXT NOT NULL,
            timeframe   TEXT NOT NULL,
            strike_price REAL,
            final_price REAL,
            outcome     TEXT NOT NULL,
            price_diff  REAL,
            price_diff_pct REAL,
            created_at  REAL NOT NULL
        );

        CREATE VIEW IF NOT EXISTS signal_performance AS
        SELECT
            s.id,
            s.market_id,
            s.coin,
            s.timeframe,
            s.action,
            s.confidence,
            s.reason,
            s.price       AS entry_pm_price,
            s.strike_price,
            s.score,
            s.trend,
            s.pm_up,
            s.pm_dn,
            s.rsi,
            s.macd_hist,
            s.obi,
            s.created_at  AS signal_time,
            o.final_price,
            o.outcome,
            o.price_diff,
            o.price_diff_pct,
            o.created_at  AS outcome_time,
            CASE
                WHEN o.outcome IS NULL THEN NULL
                WHEN (s.action = 'BUY_YES' AND o.outcome = 'UP')
                  OR (s.action = 'BUY_NO'  AND o.outcome = 'DOWN')
                THEN 'WIN'
                ELSE 'LOSS'
            END AS result,
            CASE
                WHEN o.outcome IS NULL THEN NULL
                WHEN (s.action = 'BUY_YES' AND o.outcome = 'UP')
                  OR (s.action = 'BUY_NO'  AND o.outcome = 'DOWN')
                THEN ROUND((1.0 - COALESCE(
                    CASE WHEN s.action = 'BUY_YES' THEN s.pm_up ELSE s.pm_dn END,
                    0.5
                )) / COALESCE(
                    CASE WHEN s.action = 'BUY_YES' THEN s.pm_up ELSE s.pm_dn END,
                    0.5
                ), 4)
                ELSE -1.0
            END AS roi
        FROM signals s
        LEFT JOIN market_outcomes o ON s.market_id = o.market_id;
    """)
    conn.close()


# ── Sync CRUD ────────────────────────────────────────────────────

def log_signal(
    market_id: str, coin: str, timeframe: str,
    action: str, confidence: str | None, reason: str | None,
    price: float | None, strike_price: float | None,
    score: int, trend: str,
    pm_up: float | None, pm_dn: float | None,
    time_remaining: float | None,
    rsi: float | None, macd_hist: float | None, obi: float | None,
):
    """Insert a signal row (ignores duplicates via UNIQUE constraint)."""
    conn = _connect()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO signals
                (market_id, coin, timeframe, action, confidence, reason,
                 price, strike_price, score, trend, pm_up, pm_dn,
                 time_remaining, rsi, macd_hist, obi, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            market_id, coin, timeframe, action, confidence, reason,
            price, strike_price, score, trend, pm_up, pm_dn,
            time_remaining, rsi, macd_hist, obi, time.time(),
        ))
        conn.commit()
    finally:
        conn.close()


def log_outcome(
    market_id: str, coin: str, timeframe: str,
    strike_price: float | None, final_price: float,
):
    """Insert a market outcome row (ignores duplicates)."""
    outcome = "UP" if strike_price is not None and final_price >= strike_price else "DOWN"
    price_diff = (final_price - strike_price) if strike_price is not None else None
    price_diff_pct = (price_diff / strike_price * 100) if strike_price else None

    conn = _connect()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO market_outcomes
                (market_id, coin, timeframe, strike_price, final_price,
                 outcome, price_diff, price_diff_pct, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            market_id, coin, timeframe, strike_price, final_price,
            outcome, price_diff, price_diff_pct, time.time(),
        ))
        conn.commit()
    finally:
        conn.close()


def get_stats(coin: str | None = None, timeframe: str | None = None,
              confidence: str | None = None) -> dict:
    """Query aggregated performance stats with optional filters."""
    conn = _connect()
    try:
        where_parts = ["result IS NOT NULL"]
        params: list = []

        if coin:
            where_parts.append("coin = ?")
            params.append(coin)
        if timeframe:
            where_parts.append("timeframe = ?")
            params.append(timeframe)
        if confidence:
            where_parts.append("confidence = ?")
            params.append(confidence)

        where = " AND ".join(where_parts)

        row = conn.execute(f"""
            SELECT
                COUNT(*)                              AS total,
                SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) AS losses,
                AVG(roi)                              AS avg_roi
            FROM signal_performance
            WHERE {where}
        """, params).fetchone()

        total = row["total"] or 0
        wins = row["wins"] or 0
        losses = row["losses"] or 0

        # Pending signals (no outcome yet)
        pending_where = " AND ".join([p for p in where_parts] + ["1=0"])  # hack
        # Just count pending separately
        pending_parts = ["result IS NULL"]
        pending_params: list = []
        if coin:
            pending_parts.append("coin = ?")
            pending_params.append(coin)
        if timeframe:
            pending_parts.append("timeframe = ?")
            pending_params.append(timeframe)
        if confidence:
            pending_parts.append("confidence = ?")
            pending_params.append(confidence)

        pending_row = conn.execute(f"""
            SELECT COUNT(*) AS pending
            FROM signal_performance
            WHERE {" AND ".join(pending_parts)}
        """, pending_params).fetchone()

        # Breakdown by coin
        by_coin = [dict(r) for r in conn.execute(f"""
            SELECT coin,
                   COUNT(*) AS total,
                   SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) AS wins,
                   AVG(roi) AS avg_roi
            FROM signal_performance
            WHERE {where}
            GROUP BY coin ORDER BY total DESC
        """, params).fetchall()]

        # Breakdown by timeframe
        by_timeframe = [dict(r) for r in conn.execute(f"""
            SELECT timeframe,
                   COUNT(*) AS total,
                   SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) AS wins,
                   AVG(roi) AS avg_roi
            FROM signal_performance
            WHERE {where}
            GROUP BY timeframe ORDER BY total DESC
        """, params).fetchall()]

        # Breakdown by confidence
        by_confidence = [dict(r) for r in conn.execute(f"""
            SELECT confidence,
                   COUNT(*) AS total,
                   SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) AS wins,
                   AVG(roi) AS avg_roi
            FROM signal_performance
            WHERE {where}
            GROUP BY confidence ORDER BY total DESC
        """, params).fetchall()]

        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "pending": pending_row["pending"] or 0,
            "win_rate": round(wins / total, 4) if total else None,
            "avg_roi": round(row["avg_roi"], 4) if row["avg_roi"] is not None else None,
            "by_coin": by_coin,
            "by_timeframe": by_timeframe,
            "by_confidence": by_confidence,
        }
    finally:
        conn.close()


def get_signals(coin: str | None = None, timeframe: str | None = None,
                limit: int = 50) -> list[dict]:
    """Fetch recent signals with their outcomes."""
    conn = _connect()
    try:
        where_parts = ["1=1"]
        params: list = []

        if coin:
            where_parts.append("coin = ?")
            params.append(coin)
        if timeframe:
            where_parts.append("timeframe = ?")
            params.append(timeframe)

        where = " AND ".join(where_parts)
        params.append(limit)

        rows = conn.execute(f"""
            SELECT market_id, coin, timeframe, action, confidence, reason,
                   entry_pm_price, strike_price, score, trend, pm_up, pm_dn,
                   rsi, macd_hist, obi,
                   signal_time, outcome, result, roi, final_price,
                   price_diff_pct
            FROM signal_performance
            WHERE {where}
            ORDER BY signal_time DESC
            LIMIT ?
        """, params).fetchall()

        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Async wrappers ───────────────────────────────────────────────

async def async_log_signal(*args, **kwargs):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: log_signal(*args, **kwargs))


async def async_log_outcome(*args, **kwargs):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: log_outcome(*args, **kwargs))


async def async_get_stats(*args, **kwargs) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: get_stats(*args, **kwargs))


async def async_get_signals(*args, **kwargs) -> list[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: get_signals(*args, **kwargs))
