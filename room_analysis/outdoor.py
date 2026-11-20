"""Outdoor temperature from the local data/outdoor.db (filled by scripts/ingest_outdoor.py)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "outdoor.db"


def available() -> dict | None:
    """Station, first and last hour and row count, or None when no data has been ingested."""
    if not DB_PATH.exists():
        return None
    try:
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as con:
            first, last, n = con.execute("SELECT MIN(timestamp), MAX(timestamp), COUNT(*) FROM outdoor").fetchone()
            station = (con.execute("SELECT value FROM meta WHERE key='station'").fetchone() or [""])[0]
        return {"start": pd.Timestamp(first), "end": pd.Timestamp(last), "rows": n, "station": station} if n else None
    except Exception:
        return None


def load(start, end) -> tuple[pd.Series, str] | None:
    """Hourly outdoor temperature between start and end (local time), plus the station label."""
    info = available()
    if not info:
        return None
    start, end = pd.Timestamp(start).floor("h"), pd.Timestamp(end).ceil("h")
    try:
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as con:
            df = pd.read_sql_query("SELECT timestamp, temperature FROM outdoor WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                                   con, params=[start.isoformat(), end.isoformat()])
    except Exception:
        return None
    if df.empty:
        return None
    series = pd.Series(df["temperature"].values, index=pd.to_datetime(df["timestamp"]), dtype=float)
    return series, info["station"]
