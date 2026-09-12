from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Iterable

import pandas as pd

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DATA_DIR.mkdir(exist_ok=True)


def db_path(kind: str) -> Path:
    return DATA_DIR / ("temperature.db" if kind == "Temperature" else "co2.db")


def canonical_building(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "Unassigned"
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return "Unassigned"
    if re.fullmatch(r"\d+(?:\.0+)?", text):
        return str(int(float(text)))
    return text


def canonical_room(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "Unassigned"
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return "Unassigned"
    # HFT numeric room labels are normalized to three digits so, for example,
    # CO2 mapping room 20 aligns with temperature workbook room 020.
    if re.fullmatch(r"\d+(?:\.0+)?", text):
        return f"{int(float(text)):03d}"
    return text


def canonical_floor(value=None, room=None) -> str:
    """Normalize an explicit floor or infer it from common HFT room labels."""
    if value is not None and not (isinstance(value, float) and pd.isna(value)):
        text = str(value).strip()
        if text and text.lower() not in {"nan", "unassigned"}:
            if re.fullmatch(r"-?\d+(?:\.0+)?", text):
                number = int(float(text))
                return "Ground floor" if number == 0 else f"Floor {number}"
            low = text.lower()
            if re.search(r"(?:^|[_\s-])(eg|ground|erdgeschoss)(?:$|[_\s-])", low):
                return "Ground floor"
            match = re.search(r"(?:^|[_\s-])og\s*0*(\d+)(?:$|[_\s-])", low)
            if match:
                number = int(match.group(1))
                return "Ground floor" if number == 0 else f"Floor {number}"
            match = re.search(r"(?:^|[_\s-])ug\s*0*(\d+)(?:$|[_\s-])", low)
            if match:
                return f"Floor {-int(match.group(1))}"
            return text

    label = canonical_room(room)
    if label == "Unassigned":
        return "Unassigned"
    low = label.lower()
    if re.search(r"\b(eg|ground|erdgeschoss)\b", low):
        return "Ground floor"
    match = re.search(r"(?:stock|floor|og)\s*\(?\s*(-?\d+)", low)
    if match:
        number = int(match.group(1))
        return "Ground floor" if number == 0 else f"Floor {number}"
    match = re.match(r"(\d)", label)
    if match:
        number = int(match.group(1))
        return "Ground floor" if number == 0 else f"Floor {number}"
    return "Unassigned"


def _ensure_floor_column(con: sqlite3.Connection) -> None:
    columns = {row[1] for row in con.execute("PRAGMA table_info(readings)")}
    if "floor" not in columns:
        con.execute("ALTER TABLE readings ADD COLUMN floor TEXT NOT NULL DEFAULT 'Unassigned'")
    rows = con.execute(
        "SELECT DISTINCT building, room FROM readings WHERE floor IS NULL OR floor='' OR floor='Unassigned'"
    ).fetchall()
    for building, room in rows:
        floor = canonical_floor(room=room)
        if floor != "Unassigned":
            con.execute(
                "UPDATE readings SET floor=? WHERE building=? AND room=? AND (floor IS NULL OR floor='' OR floor='Unassigned')",
                (floor, building, room),
            )
    con.commit()


def connect(kind: str) -> sqlite3.Connection:
    """Open a lightweight connection; schema migrations belong in init_db()."""
    con = sqlite3.connect(db_path(kind), timeout=60)
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init_db(kind: str, replace: bool = False, with_indexes: bool = True) -> sqlite3.Connection:
    con = connect(kind)
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    if replace:
        con.execute("DROP TABLE IF EXISTS readings")
        con.execute("DROP TABLE IF EXISTS metadata")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS readings (
            timestamp TEXT NOT NULL,
            sensor TEXT NOT NULL,
            building TEXT NOT NULL DEFAULT 'Unassigned',
            floor TEXT NOT NULL DEFAULT 'Unassigned',
            room TEXT NOT NULL DEFAULT 'Unassigned',
            value REAL NOT NULL,
            secondary_temperature REAL,
            humidity REAL,
            source TEXT NOT NULL DEFAULT 'Historical'
        );
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    if with_indexes:
        create_indexes(con)
    return con


def create_indexes(con: sqlite3.Connection) -> None:
    _ensure_floor_column(con)
    con.execute("CREATE INDEX IF NOT EXISTS idx_readings_brt ON readings(building, room, timestamp)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_readings_bfrst ON readings(building, floor, room, sensor, timestamp)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_readings_sensor_time ON readings(sensor, timestamp)")
    con.commit()


def ensure_database_ready(kind: str) -> None:
    """Run idempotent schema/index migrations outside ordinary read connections."""
    if not db_path(kind).exists():
        return
    with connect(kind) as con:
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='readings'").fetchone():
            create_indexes(con)


def insert_rows(con: sqlite3.Connection, rows: Iterable[tuple], batch: int = 100000) -> int:
    sql = """INSERT INTO readings
        (timestamp, sensor, building, room, value, secondary_temperature, humidity, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""
    buf, total = [], 0
    for row in rows:
        buf.append(row)
        if len(buf) >= batch:
            con.executemany(sql, buf)
            con.commit()
            total += len(buf)
            buf.clear()
    if buf:
        con.executemany(sql, buf)
        con.commit()
        total += len(buf)
    return total


def exists(kind: str) -> bool:
    path = db_path(kind)
    if not path.exists():
        return False
    try:
        with sqlite3.connect(path) as con:
            return con.execute("SELECT 1 FROM readings LIMIT 1").fetchone() is not None
    except Exception:
        return False


def summary(kind: str) -> dict:
    if not exists(kind):
        return {}
    with connect(kind) as con:
        row = con.execute(
            """SELECT COUNT(*), COUNT(DISTINCT sensor), COUNT(DISTINCT building),
               COUNT(DISTINCT building || '|' || room), MIN(timestamp), MAX(timestamp),
               MIN(value), MAX(value) FROM readings"""
        ).fetchone()
    return dict(zip(["rows", "sensors", "buildings", "rooms", "start", "end", "min", "max"], row))


def hierarchy(kind: str) -> pd.DataFrame:
    if not exists(kind):
        return pd.DataFrame(columns=["building", "floor", "room", "sensor"])
    with connect(kind) as con:
        return pd.read_sql_query(
            "SELECT DISTINCT building, floor, room, sensor FROM readings ORDER BY building, floor, room, sensor", con
        )


def room_span(kind: str, building: str, room: str) -> dict:
    if not exists(kind):
        return {}
    with connect(kind) as con:
        row = con.execute(
            "SELECT MIN(timestamp), MAX(timestamp), COUNT(*) FROM readings WHERE building=? AND room=?",
            (building, room),
        ).fetchone()
    if not row or not row[0]:
        return {}
    return {"start": row[0], "end": row[1], "rows": row[2]}


def query(kind: str, building: str, room: str, start=None, end=None) -> pd.DataFrame:
    clauses = ["building = ?", "room = ?"]
    params = [building, room]
    if start is not None:
        clauses.append("timestamp >= ?")
        params.append(pd.Timestamp(start).isoformat())
    if end is not None:
        clauses.append("timestamp <= ?")
        params.append(pd.Timestamp(end).isoformat())
    sql = f"SELECT timestamp, sensor, building, floor, room, value, secondary_temperature, humidity, source FROM readings WHERE {' AND '.join(clauses)} ORDER BY timestamp"
    with connect(kind) as con:
        df = pd.read_sql_query(sql, con, params=params)
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df = df.dropna(subset=["timestamp", "value"])
    return df


def mapping_table(kind: str) -> pd.DataFrame:
    h = hierarchy(kind)
    if h.empty:
        return pd.DataFrame(columns=["sensor", "building", "floor", "room", "display_name"])
    m = h[["sensor", "building", "floor", "room"]].drop_duplicates("sensor").copy()
    m["display_name"] = m["sensor"]
    return m[["sensor", "building", "floor", "room", "display_name"]]


def normalize_mapping(mapping: pd.DataFrame) -> pd.DataFrame:
    """Accept either dashboard mapping columns or the HFT Standort workbook columns."""
    if mapping is None or mapping.empty:
        return pd.DataFrame(columns=["sensor", "building", "room"])
    aliases = {}
    for c in mapping.columns:
        low = str(c).strip().lower()
        if low in {"sensor", "sensor_id", "id"}:
            aliases[c] = "sensor"
        elif low in {"building", "bau", "gebäude", "gebaeude"}:
            aliases[c] = "building"
        elif low in {"floor", "storey", "story", "geschoss", "etage"}:
            aliases[c] = "floor"
        elif low in {"room", "raum"}:
            aliases[c] = "room"
    m = mapping.rename(columns=aliases).copy()
    required = {"sensor", "building", "room"}
    if not required.issubset(m.columns):
        raise ValueError("Mapping must contain Sensor/ID, Building/Bau and Room/Raum columns. Floor/Geschoss is optional.")
    columns = ["sensor", "building", "room"] + (["floor"] if "floor" in m.columns else [])
    m = m[columns].dropna(subset=["sensor"]).copy()
    m["sensor"] = m["sensor"].astype(str).str.strip()
    m["building"] = m["building"].map(canonical_building)
    m["room"] = m["room"].map(canonical_room)
    if "floor" in m.columns:
        m["floor"] = [canonical_floor(value, room) for value, room in zip(m["floor"], m["room"])]
    else:
        m["floor"] = m["room"].map(lambda room: canonical_floor(room=room))
    return m[m["sensor"] != ""].drop_duplicates("sensor", keep="last")


def apply_mapping(kind: str, mapping: pd.DataFrame) -> int:
    m = normalize_mapping(mapping)
    if m.empty:
        return 0
    updates = [(r.building, r.floor, r.room, r.sensor) for r in m.itertuples(index=False)]
    matched = 0
    with connect(kind) as con:
        for building, floor, room, sensor in updates:
            cur = con.execute(
                "UPDATE readings SET building=?, floor=?, room=? WHERE lower(sensor)=lower(?)",
                (building, floor, room, sensor),
            )
            matched += max(cur.rowcount, 0)
        con.commit()
    return matched


def latest_by_room(kind: str, building: str, floor: str | None = None, room: str | None = None) -> pd.DataFrame:
    """Return the latest individual sensor reading for each room in a scope."""
    if not exists(kind):
        return pd.DataFrame(columns=["timestamp", "sensor", "building", "floor", "room", "value"])
    filters = [("building", building)]
    if floor is not None:
        filters.append(("floor", floor))
    if room is not None:
        filters.append(("room", room))
    inner_where = " AND ".join(f"src.{column}=?" for column, _ in filters)
    outer_where = " AND ".join(f"r.{column}=?" for column, _ in filters)
    values = [value for _, value in filters]
    sql = f"""
        SELECT r.timestamp, r.sensor, r.building, r.floor, r.room, r.value
        FROM readings r
        JOIN (
            SELECT src.building, src.floor, src.room, src.sensor, MAX(src.timestamp) AS timestamp
            FROM readings src WHERE {inner_where}
            GROUP BY src.building, src.floor, src.room, src.sensor
        ) latest ON r.building=latest.building
            AND r.floor=latest.floor
            AND r.room=latest.room
            AND r.sensor=latest.sensor
            AND r.timestamp=latest.timestamp
        WHERE {outer_where}
        ORDER BY r.floor, r.room, r.sensor
    """
    with connect(kind) as con:
        df = pd.read_sql_query(sql, con, params=values + values)
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    return df


def mapping_coverage(kind: str) -> dict:
    if not exists(kind):
        return {"sensors": 0, "mapped": 0, "unassigned": 0}
    with connect(kind) as con:
        sensors = con.execute("SELECT COUNT(DISTINCT sensor) FROM readings").fetchone()[0]
        mapped = con.execute(
            "SELECT COUNT(DISTINCT sensor) FROM readings WHERE building <> 'Unassigned' AND room <> 'Unassigned'"
        ).fetchone()[0]
    return {"sensors": sensors, "mapped": mapped, "unassigned": sensors - mapped}


def metadata_get(kind: str, key: str, default=None):
    if not db_path(kind).exists():
        return default
    try:
        with connect(kind) as con:
            row = con.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else default
    except Exception:
        return default


def metadata_set(kind: str, key: str, value) -> None:
    with connect(kind) as con:
        con.execute(
            "INSERT INTO metadata(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        con.commit()
