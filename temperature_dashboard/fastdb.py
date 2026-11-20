"""Fast read path: the compact copy built by scripts/build_grafana_db.py (data/grafana/hft_sensors.db).

Integer timestamps, one index per room and hourly summaries make selectors and charts load in milliseconds
instead of scanning the 10 M-row source database. The copy is used only while the source file still has the size
it had when the copy was built; after adding data, re-run build_grafana_db.py (otherwise the slow path is used).
Windows up to RAW_MAX_DAYS return raw readings, longer windows hourly means (df.attrs["resolution"] == "hourly").
"""
from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path

import pandas as pd

FAST_DB = Path(__file__).resolve().parents[1] / "data" / "grafana" / "hft_sensors.db"
RAW_MAX_DAYS = 31
_KIND = {  # kind -> (meta key, raw table, hourly table)
    "CO₂": ("co2", "co2_raw", "co2_hourly"),
    "Temperature": ("temp", "temp_raw", "temp_hourly"),
}


def _con() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{FAST_DB.as_posix()}?mode=ro", uri=True, timeout=30)


@lru_cache(maxsize=16)
def _usable(kind: str, fast_mtime: int, src_size: int) -> bool:
    try:
        with _con() as con:
            row = con.execute("SELECT value FROM meta WHERE key=?", (f"src_size_{_KIND[kind][0]}",)).fetchone()
            has_catalog = con.execute("SELECT 1 FROM sqlite_master WHERE name='catalog'").fetchone()
        return bool(row and has_catalog and row[0] == str(src_size))
    except Exception:
        return False


def available(kind: str, src_path: Path) -> bool:
    if kind not in _KIND or not FAST_DB.exists() or not Path(src_path).exists():
        return False
    return _usable(kind, FAST_DB.stat().st_mtime_ns, Path(src_path).stat().st_size)


def _ts(value) -> int:
    return int(pd.Timestamp(value).tz_localize("UTC").timestamp())


def hierarchy(kind: str) -> pd.DataFrame:
    with _con() as con:
        return pd.read_sql_query(
            "SELECT building, floor, room, sensor FROM catalog WHERE kind=? ORDER BY building, floor, room, sensor",
            con, params=(kind,))


def room_span(kind: str, building: str, room: str) -> dict:
    with _con() as con:
        row = con.execute("SELECT MIN(first_ts), MAX(last_ts), SUM(n) FROM catalog WHERE kind=? AND building=? AND room=?",
                          (kind, building, room)).fetchone()
    if not row or row[0] is None:
        return {}
    return {"start": pd.to_datetime(row[0], unit="s").isoformat(), "end": pd.to_datetime(row[1] + 3599, unit="s").isoformat(), "rows": int(row[2])}


def query(kind: str, building: str, room: str, start=None, end=None) -> pd.DataFrame:
    _, raw, hourly = _KIND[kind]
    span = room_span(kind, building, room)
    if not span:
        return pd.DataFrame()
    lo = _ts(start) if start is not None else _ts(span["start"])
    hi = _ts(end) if end is not None else _ts(span["end"])
    use_raw = (hi - lo) <= RAW_MAX_DAYS * 86400
    where = "building=? AND room=? AND ts>=? AND ts<=?"
    params = (building, room, lo, hi)
    if kind == "CO₂":
        sql = (f"SELECT ts, sensor, building, floor, room, co2 AS value, temp_c AS secondary_temperature, humidity FROM {raw} WHERE {where} ORDER BY ts"
               if use_raw else
               f"SELECT ts, sensor, building, floor, room, co2_mean AS value, temp_c AS secondary_temperature, humidity FROM {hourly} WHERE {where} ORDER BY ts")
    else:
        sql = (f"SELECT ts, sensor, building, floor, room, temp_c AS value, NULL AS secondary_temperature, NULL AS humidity FROM {raw} WHERE {where} ORDER BY ts"
               if use_raw else
               f"SELECT ts, sensor, building, floor, room, temp_c AS value, NULL AS secondary_temperature, NULL AS humidity FROM {hourly} WHERE {where} ORDER BY ts")
    with _con() as con:
        df = pd.read_sql_query(sql, con, params=params)
    df["timestamp"] = pd.to_datetime(df.pop("ts"), unit="s")
    df["source"] = "fast-copy" if use_raw else "fast-copy (hourly mean)"
    df.attrs["resolution"] = "raw" if use_raw else "hourly"
    return df[["timestamp", "sensor", "building", "floor", "room", "value", "secondary_temperature", "humidity", "source"]]
