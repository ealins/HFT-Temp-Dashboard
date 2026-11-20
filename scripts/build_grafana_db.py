"""Build a small, fast, read-only database for Grafana from the dashboard databases.

    python scripts\\build_grafana_db.py

Reads data/co2.db, data/temperature.db and (if present) data/outdoor.db and writes
data/grafana/hft_sensors.db plus grafana/dashboards/hft-rooms.json.

Why a separate database?
- Timestamps become integer unix seconds (Grafana's SQLite plugin reads them directly, no text parsing).
- Rows are stored clustered by (building, room, time), so one room's time range is a single index range scan.
- Hourly summary tables serve long time ranges (a year of one room is ~8,700 rows instead of ~100,000).
- The big source files stay untouched. Re-run the script whenever you add data.

Time zone: the sensor timestamps are local clock times without a zone. They are stored as if they were UTC,
and the dashboard is set to the UTC time zone, so the clock times shown in Grafana equal your readings.

NOTE: the file must not be called grafana.db (the plugin blocks that name).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
OUT_DIR = DATA_DIR / "grafana"
OUT_DB = OUT_DIR / "hft_sensors.db"
DASHBOARD_DIR = PROJECT_ROOT / "grafana" / "dashboards"
DASHBOARD_FILE = DASHBOARD_DIR / "hft-rooms.json"
DS = {"type": "frser-sqlite-datasource", "uid": "hft-sqlite"}
RAW_LIMIT_MS = 7 * 86400 * 1000       # ranges up to 7 days use raw readings, longer ranges use hourly means

SCHEMA = """
CREATE TABLE co2_raw (
    building TEXT NOT NULL, room TEXT NOT NULL, ts INTEGER NOT NULL,
    floor TEXT, co2 REAL, temp_c REAL, humidity REAL,
    PRIMARY KEY (building, room, ts)) WITHOUT ROWID;
CREATE TABLE temp_raw (
    building TEXT NOT NULL, room TEXT NOT NULL, ts INTEGER NOT NULL,
    floor TEXT, temp_c REAL,
    PRIMARY KEY (building, room, ts)) WITHOUT ROWID;
CREATE TABLE co2_hourly (
    building TEXT NOT NULL, room TEXT NOT NULL, ts INTEGER NOT NULL,
    floor TEXT, co2_mean REAL, co2_max REAL, temp_c REAL, humidity REAL, n INTEGER,
    PRIMARY KEY (building, room, ts)) WITHOUT ROWID;
CREATE TABLE temp_hourly (
    building TEXT NOT NULL, room TEXT NOT NULL, ts INTEGER NOT NULL,
    floor TEXT, temp_c REAL, temp_min REAL, temp_max REAL, n INTEGER,
    PRIMARY KEY (building, room, ts)) WITHOUT ROWID;
CREATE TABLE outdoor (ts INTEGER PRIMARY KEY, temp_c REAL, humidity REAL);
CREATE TABLE rooms (
    building TEXT NOT NULL, floor TEXT, room TEXT NOT NULL,
    has_co2 INTEGER, has_temp INTEGER, first_ts INTEGER, last_ts INTEGER,
    PRIMARY KEY (building, room));
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def step(msg: str, t0: float) -> None:
    print(f"[{time.monotonic() - t0:6.0f} s] {msg}", flush=True)


def build(include_unassigned: bool) -> dict:
    t0 = time.monotonic()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT_DB.with_suffix(".tmp")
    for leftover in (tmp, Path(str(tmp) + "-journal")):
        if leftover.exists():
            leftover.unlink()
    con = sqlite3.connect(tmp, uri=True)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA cache_size=-200000")
    con.executescript(SCHEMA)
    unassigned = "" if include_unassigned else "AND building NOT IN ('Unassigned', '') AND room NOT IN ('Unassigned', '')"

    co2_path, temp_path, out_path = DATA_DIR / "co2.db", DATA_DIR / "temperature.db", DATA_DIR / "outdoor.db"
    if co2_path.exists():
        con.execute(f"ATTACH DATABASE 'file:{co2_path.as_posix()}?mode=ro' AS src_co2")
        step("Copying CO2 readings (largest step, a few minutes)…", t0)
        # ORDER BY matches the source index (building, room, timestamp) so the rows arrive pre-sorted for the clustered key
        con.execute(f"""
            INSERT OR REPLACE INTO co2_raw
            SELECT building, room, CAST(strftime('%s', timestamp) AS INTEGER), floor, value,
                   CASE WHEN secondary_temperature BETWEEN -40 AND 60 THEN secondary_temperature END,
                   CASE WHEN humidity BETWEEN 0 AND 100 THEN humidity END
            FROM src_co2.readings
            WHERE value > 0 AND value < 20000 {unassigned}
            ORDER BY building, room, timestamp""")
        con.commit()
        con.execute("DETACH DATABASE src_co2")
    n_co2 = con.execute("SELECT COUNT(*) FROM co2_raw").fetchone()[0]
    step(f"{n_co2:,} CO2 readings", t0)

    if temp_path.exists():
        con.execute(f"ATTACH DATABASE 'file:{temp_path.as_posix()}?mode=ro' AS src_temp")
        step("Copying temperature readings…", t0)
        con.execute(f"""
            INSERT OR REPLACE INTO temp_raw
            SELECT building, room, CAST(strftime('%s', timestamp) AS INTEGER), floor, value
            FROM src_temp.readings
            WHERE value BETWEEN -40 AND 60 {unassigned}
            ORDER BY building, room, timestamp""")
        con.commit()
        con.execute("DETACH DATABASE src_temp")
    n_temp = con.execute("SELECT COUNT(*) FROM temp_raw").fetchone()[0]
    step(f"{n_temp:,} temperature readings", t0)

    step("Building hourly summaries…", t0)
    con.execute("""
        INSERT INTO co2_hourly
        SELECT building, room, ts / 3600 * 3600, MAX(floor), AVG(co2), MAX(co2), AVG(temp_c), AVG(humidity), COUNT(*)
        FROM co2_raw GROUP BY building, room, ts / 3600 ORDER BY building, room, ts / 3600""")
    con.execute("""
        INSERT INTO temp_hourly
        SELECT building, room, ts / 3600 * 3600, MAX(floor), AVG(temp_c), MIN(temp_c), MAX(temp_c), COUNT(*)
        FROM temp_raw GROUP BY building, room, ts / 3600 ORDER BY building, room, ts / 3600""")

    if out_path.exists():
        con.execute(f"ATTACH DATABASE 'file:{out_path.as_posix()}?mode=ro' AS src_out")
        con.execute("""INSERT OR REPLACE INTO outdoor
                       SELECT CAST(strftime('%s', timestamp) AS INTEGER), temperature, humidity FROM src_out.outdoor""")
        station = (con.execute("SELECT value FROM src_out.meta WHERE key='station'").fetchone() or [""])[0]
        con.commit()
        con.execute("DETACH DATABASE src_out")
        con.execute("INSERT INTO meta VALUES ('outdoor_station', ?)", (station,))
    n_out = con.execute("SELECT COUNT(*) FROM outdoor").fetchone()[0]

    con.execute("""
        INSERT INTO rooms
        SELECT building, MAX(floor), room, MAX(has_co2), MAX(has_temp), MIN(first_ts), MAX(last_ts) FROM (
            SELECT building, floor, room, 1 AS has_co2, 0 AS has_temp, MIN(ts) AS first_ts, MAX(ts) AS last_ts
              FROM co2_hourly GROUP BY building, room
            UNION ALL
            SELECT building, floor, room, 0, 1, MIN(ts), MAX(ts) FROM temp_hourly GROUP BY building, room)
        GROUP BY building, room""")
    con.execute("INSERT INTO meta VALUES ('built', datetime('now'))")
    con.execute("INSERT INTO meta VALUES ('timezone_note', 'timestamps are local clock time stored as UTC')")
    con.commit()
    step("Analysing…", t0)
    con.execute("ANALYZE")
    rooms = con.execute("SELECT building, floor, room, has_co2, has_temp, last_ts FROM rooms ORDER BY building, room").fetchall()
    con.commit()
    con.close()
    if OUT_DB.exists():
        OUT_DB.unlink()
    tmp.replace(OUT_DB)
    step(f"Written {OUT_DB} ({OUT_DB.stat().st_size / 1e6:.0f} MB)", t0)
    return {"rooms": rooms, "n_co2": n_co2, "n_temp": n_temp, "n_out": n_out}


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------
def _sql_range(table_raw: str, table_hour: str, raw_cols: str, hour_cols: str, where: str) -> str:
    """Raw readings for short ranges, hourly means for long ones."""
    rng = f"ts >= $__from / 1000 AND ts <= $__to / 1000"
    return (f"SELECT time, value FROM (\n"
            f"  SELECT ts AS time, {raw_cols} AS value FROM {table_raw} WHERE {where} AND {rng} AND ($__to - $__from) <= {RAW_LIMIT_MS}\n"
            f"  UNION ALL\n"
            f"  SELECT ts AS time, {hour_cols} AS value FROM {table_hour} WHERE {where} AND {rng} AND ($__to - $__from) > {RAW_LIMIT_MS}\n"
            f") ORDER BY time")


ROOM_WHERE = "building = '$building' AND room = '$room'"
# fallback (--flat-pickers): one combined "building/room" picker
ROOM_WHERE_FLAT = ("building = substr('$room', 1, instr('$room', '/') - 1) "
                   "AND room = substr('$room', instr('$room', '/') + 1)")


def _target(ref: str, sql: str, kind: str = "time series", name: str | None = None) -> dict:
    if name:
        sql = sql.replace("AS value", f'AS "{name}"').replace("SELECT time, value FROM", f'SELECT time, "{name}" FROM')
    return {"refId": ref, "datasource": DS, "queryType": kind, "queryText": sql, "rawQueryText": sql,
            "timeColumns": ["time"]}


def _panel(pid: int, title: str, ptype: str, x: int, y: int, w: int, h: int, targets: list[dict], **extra) -> dict:
    panel = {"id": pid, "title": title, "type": ptype, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
             "targets": targets}
    panel.update(extra)
    return panel


def _var_query(sql: str) -> dict:
    return {"queryText": sql, "rawQueryText": sql, "queryType": "table", "timeColumns": []}


def templating(labels: list, default: tuple, flat: bool) -> list:
    """Building -> Floor -> Room pickers (query variables), or one flat room list with --flat-pickers."""
    if flat:
        room_query = ",".join(f"{text} : {value}" for text, value, _, _ in labels)
        buildings = ",".join(sorted({v.split("/")[0] for _, v, _, _ in labels}))
        return [
            {"name": "room", "label": "Room", "type": "custom", "query": room_query,
             "current": {"text": default[0], "value": default[1], "selected": True}, "options": [], "multi": False, "includeAll": False},
            {"name": "building", "label": "Building (tables)", "type": "custom", "query": buildings,
             "current": {"text": default[1].split("/")[0], "value": default[1].split("/")[0], "selected": True},
             "options": [], "multi": False, "includeAll": False}]
    common = {"type": "query", "datasource": DS, "refresh": 1, "sort": 1, "multi": False, "includeAll": False, "options": []}
    return [
        {**common, "name": "building", "label": "Building",
         "query": _var_query("SELECT DISTINCT building FROM rooms ORDER BY building"), "current": {}},
        {**common, "name": "floor", "label": "Floor", "sort": 0,
         "query": _var_query("SELECT floor FROM rooms WHERE building = '$building' GROUP BY floor ORDER BY MIN(room)"), "current": {}},
        {**common, "name": "room", "label": "Room",
         "query": _var_query("SELECT room FROM rooms WHERE building = '$building' AND floor = '$floor' ORDER BY room"), "current": {}},
    ]


def dashboard_json(rooms: list[tuple], station: str, flat: bool = False) -> dict:
    room_where = ROOM_WHERE_FLAT if flat else ROOM_WHERE
    ok = [r for r in rooms if not re.search(r"[,:/]", f"{r[0]}{r[2]}")]
    labels = []
    for building, floor, room, has_co2, has_temp, last_ts in sorted(ok, key=lambda r: (-(r[5] or 0) * 0, r[0], r[2])):
        sensors = "+".join(s for s, flag in (("CO2", has_co2), ("T", has_temp)) if flag)
        labels.append((f"Bau {building} - {room} ({floor}, {sensors})", f"{building}/{room}", last_ts or 0, has_co2))
    default = max((l for l in labels if l[3]), key=lambda l: l[2], default=labels[0] if labels else ("", "1/001", 0, 0))
    room_query = ",".join(f"{text} : {value}" for text, value, _, _ in labels)
    buildings = sorted({r[0] for r in ok})
    building_query = ",".join(buildings)
    time_rng = "ts >= $__from / 1000 AND ts <= $__to / 1000"
    bwhere = "building = '$building'"

    co2 = _target("A", _sql_range("co2_raw", "co2_hourly", "co2", "co2_mean", room_where), name="CO2 (ppm)")
    t_room = _target("A", _sql_range("temp_raw", "temp_hourly", "temp_c", "temp_c", room_where), name="Room temperature sensor")
    t_co2 = _target("B", _sql_range("co2_raw", "co2_hourly", "temp_c", "temp_c", room_where), name="CO2 sensor thermometer")
    t_out = _target("C", f"SELECT ts AS time, temp_c AS \"Outdoor (DWD)\" FROM outdoor WHERE {time_rng} ORDER BY ts")
    hum = _target("A", _sql_range("co2_raw", "co2_hourly", "humidity", "humidity", room_where), name="Humidity (%)")

    stat_hours = _target("A", f"SELECT COUNT(*) AS \"Hours with mean ≥ 1000 ppm\" FROM co2_hourly WHERE {room_where} AND {time_rng} AND co2_mean >= 1000", "table")
    stat_peak = _target("A", f"SELECT ROUND(MAX(co2_max)) AS \"Peak CO2 (ppm)\" FROM co2_hourly WHERE {room_where} AND {time_rng}", "table")
    stat_mean = _target("A", f"SELECT ROUND(AVG(co2_mean)) AS \"Mean CO2 (ppm)\" FROM co2_hourly WHERE {room_where} AND {time_rng}", "table")
    stat_temp = _target("A", f"SELECT ROUND(AVG(temp_c), 1) AS \"Mean room temp (°C)\" FROM (SELECT temp_c FROM temp_hourly WHERE {room_where} AND {time_rng} "
                           f"UNION ALL SELECT temp_c FROM co2_hourly WHERE {room_where} AND {time_rng})", "table")
    diurnal = _target("A", f"SELECT strftime('%H', ts, 'unixepoch') AS hour, ROUND(AVG(co2_mean)) AS \"Mean CO2 (ppm)\" "
                           f"FROM co2_hourly WHERE {room_where} AND {time_rng} GROUP BY hour ORDER BY hour", "table")
    table = _target("A", f"SELECT room AS Room, MAX(floor) AS Floor, ROUND(AVG(co2_mean)) AS \"Mean CO2\", ROUND(MAX(co2_max)) AS \"Peak CO2\", "
                         f"SUM(co2_mean >= 1000) AS \"Hours >= 1000 ppm\", ROUND(AVG(temp_c), 1) AS \"Mean C (CO2 sensor)\" "
                         f"FROM co2_hourly WHERE {bwhere} AND {time_rng} GROUP BY room ORDER BY SUM(co2_mean >= 1000) DESC", "table")
    table_t = _target("A", f"SELECT room AS Room, MAX(floor) AS Floor, ROUND(AVG(temp_c), 1) AS \"Mean C\", ROUND(MIN(temp_min), 1) AS \"Min C\", "
                           f"ROUND(MAX(temp_max), 1) AS \"Max C\", SUM(temp_c > 26) AS \"Hours > 26 C\", SUM(temp_c < 20) AS \"Hours < 20 C\" "
                           f"FROM temp_hourly WHERE {bwhere} AND {time_rng} GROUP BY room ORDER BY room", "table")

    limits = {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "orange", "value": 800}, {"color": "red", "value": 1000}]}
    ts_opts = {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}
    panels = [
        _panel(1, "Mean CO2", "stat", 0, 0, 4, 4, [stat_mean], fieldConfig={"defaults": {"unit": "ppm", "thresholds": limits}, "overrides": []}, options={"reduceOptions": {"calcs": ["lastNotNull"]}}),
        _panel(2, "Peak CO2", "stat", 4, 0, 4, 4, [stat_peak], fieldConfig={"defaults": {"unit": "ppm", "thresholds": limits}, "overrides": []}, options={"reduceOptions": {"calcs": ["lastNotNull"]}}),
        _panel(3, "Hours with mean >= 1000 ppm", "stat", 8, 0, 4, 4, [stat_hours], fieldConfig={"defaults": {"unit": "h", "decimals": 0}, "overrides": []}, options={"reduceOptions": {"calcs": ["lastNotNull"]}}),
        _panel(4, "Mean room temperature", "stat", 12, 0, 4, 4, [stat_temp], fieldConfig={"defaults": {"unit": "celsius", "decimals": 1}, "overrides": []}, options={"reduceOptions": {"calcs": ["lastNotNull"]}}),
        _panel(5, "CO2 (selected room)", "timeseries", 0, 4, 16, 9, [co2],
               fieldConfig={"defaults": {"unit": "ppm", "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "red", "value": 1000}]},
                                         "custom": {"thresholdsStyle": {"mode": "line"}, "lineWidth": 1, "spanNulls": 3600000}}, "overrides": []}, options=ts_opts),
        _panel(6, "Temperature: room, CO2 sensor and outdoor", "timeseries", 0, 13, 16, 9, [t_room, t_co2, t_out],
               fieldConfig={"defaults": {"unit": "celsius", "custom": {"lineWidth": 1, "spanNulls": 3600000}}, "overrides": []}, options=ts_opts),
        _panel(7, "Relative humidity", "timeseries", 0, 22, 16, 7, [hum], fieldConfig={"defaults": {"unit": "percent", "custom": {"lineWidth": 1, "spanNulls": 3600000}}, "overrides": []}, options=ts_opts),
        _panel(8, "Average CO2 by hour of day", "barchart", 16, 0, 8, 9, [diurnal], fieldConfig={"defaults": {"unit": "ppm"}, "overrides": []}, options={"xField": "hour", "legend": {"showLegend": False}}),
        _panel(9, "All rooms in building: CO2 (selected time range)", "table", 16, 9, 8, 10, [table], fieldConfig={"defaults": {}, "overrides": []}),
        _panel(10, "All rooms in building: temperature (selected time range)", "table", 16, 19, 8, 10, [table_t], fieldConfig={"defaults": {}, "overrides": []}),
    ]
    return {
        "uid": "hft-rooms", "title": "HFT indoor climate", "tags": ["hft"], "timezone": "utc", "schemaVersion": 39, "version": 1,
        "editable": True, "refresh": "", "time": {"from": "now-1y", "to": "now"}, "graphTooltip": 1,
        "description": f"Built by scripts/build_grafana_db.py. Times are local clock times (UTC zone on purpose). Outdoor: {station or 'none loaded'}.",
        "templating": {"list": templating(labels, default, flat)},
        "panels": panels,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build data/grafana/hft_sensors.db and the Grafana dashboard")
    ap.add_argument("--include-unassigned", action="store_true", help="Also copy readings without a building/room")
    ap.add_argument("--dashboard-only", action="store_true", help="Regenerate only the dashboard JSON from the existing database")
    ap.add_argument("--flat-pickers", action="store_true", help="One combined room dropdown instead of Building/Floor/Room")
    args = ap.parse_args()
    if args.dashboard_only:
        con = sqlite3.connect(f"file:{OUT_DB.as_posix()}?mode=ro", uri=True)
        rooms = con.execute("SELECT building, floor, room, has_co2, has_temp, last_ts FROM rooms ORDER BY building, room").fetchall()
        station = (con.execute("SELECT value FROM meta WHERE key='outdoor_station'").fetchone() or [""])[0]
        con.close()
    else:
        result = build(args.include_unassigned)
        rooms = result["rooms"]
        con = sqlite3.connect(f"file:{OUT_DB.as_posix()}?mode=ro", uri=True)
        station = (con.execute("SELECT value FROM meta WHERE key='outdoor_station'").fetchone() or [""])[0]
        con.close()
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    DASHBOARD_FILE.write_text(json.dumps(dashboard_json(rooms, station, args.flat_pickers), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Dashboard written: {DASHBOARD_FILE}  ({len(rooms)} rooms)")
    print("Next: cd grafana ; docker compose up -d ; open http://localhost:3000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
