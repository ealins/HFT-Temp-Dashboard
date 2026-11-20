"""Download outdoor air temperature from the DWD (German Weather Service) into data/outdoor.db.

Default station: Stuttgart-Echterdingen (the airport). The station id is looked up by name
from DWD's own station list, so nothing is hard-coded.

    python scripts\\ingest_outdoor.py                    # airport, history + recent
    python scripts\\ingest_outdoor.py --station "Stuttgart (Schnarrenberg)"
    python scripts\\ingest_outdoor.py --file C:\\path\\stundenwerte_TU_04931_akt.zip   # offline: a zip/txt you downloaded

Data: DWD Open Data, hourly air temperature and relative humidity
(https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/hourly/air_temperature/).
DWD times are UTC; they are converted to local Stuttgart time (naive), like the sensor data.
Re-running only adds new hours. Needs internet (except with --file).
"""
from __future__ import annotations

import argparse
import io
import re
import sqlite3
import sys
import zipfile
from pathlib import Path

import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "outdoor.db"
BASE = "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/hourly/air_temperature"
DEFAULT_STATION = "Stuttgart-Echterdingen"
LOCAL_TZ = "Europe/Berlin"


# ---------------------------------------------------------------------------
# parsing (pure, testable offline)
# ---------------------------------------------------------------------------
def parse_station_list(text: str) -> list[dict]:
    """Rows of TU_Stundenwerte_Beschreibung_Stationen.txt: id, from, to, height, lat, lon, name, state."""
    stations = []
    pattern = re.compile(r"^\s*(\d{5})\s+(\d{8})\s+(\d{8})\s+(-?\d+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(.+?)\s{2,}(\S.*?)\s*$")
    for line in text.splitlines():
        m = pattern.match(line)
        if m:
            stations.append({"id": m.group(1), "from": m.group(2), "to": m.group(3), "height_m": int(m.group(4)),
                             "lat": float(m.group(5)), "lon": float(m.group(6)), "name": m.group(7).strip(),
                             "state": m.group(8).strip()})
    return stations


def find_station(stations: list[dict], wanted: str) -> dict | None:
    key = re.sub(r"[^a-z0-9]", "", wanted.lower())
    exact = [s for s in stations if re.sub(r"[^a-z0-9]", "", s["name"].lower()) == key]
    if exact:
        return exact[0]
    partial = [s for s in stations if key in re.sub(r"[^a-z0-9]", "", s["name"].lower())]
    return partial[0] if partial else None


def parse_product(raw: bytes) -> pd.DataFrame:
    """produkt_tu_stunde_*.txt -> local time, temperature (deg C), relative humidity (%)."""
    df = pd.read_csv(io.BytesIO(raw), sep=";", skipinitialspace=True, na_values=["-999", "-999.0"], dtype=str)
    df.columns = [c.strip() for c in df.columns]
    stamp = df["MESS_DATUM"].str.strip().str[:10]
    utc = pd.to_datetime(stamp, format="%Y%m%d%H", errors="coerce")
    out = pd.DataFrame({
        "timestamp": utc.dt.tz_localize("UTC").dt.tz_convert(LOCAL_TZ).dt.tz_localize(None),
        "temperature": pd.to_numeric(df["TT_TU"], errors="coerce"),
        "humidity": pd.to_numeric(df["RF_TU"], errors="coerce") if "RF_TU" in df else None,
    })
    out = out.dropna(subset=["timestamp", "temperature"])
    out = out[out["temperature"] > -90]
    return out.drop_duplicates("timestamp")


def read_zip_or_txt(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            name = next(n for n in zf.namelist() if n.lower().startswith("produkt_tu_stunde"))
            return parse_product(zf.read(name))
    return parse_product(path.read_bytes())


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------
def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS outdoor (
        timestamp TEXT PRIMARY KEY, temperature REAL NOT NULL, humidity REAL, station TEXT NOT NULL)""")
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    return con


def store(df: pd.DataFrame, station_label: str, extra_meta: dict | None = None) -> int:
    con = connect()
    before = con.execute("SELECT COUNT(*) FROM outdoor").fetchone()[0]
    rows = [(t.strftime("%Y-%m-%dT%H:%M:%S"), float(temp), None if pd.isna(h) else float(h), station_label)
            for t, temp, h in zip(df["timestamp"], df["temperature"], df["humidity"])]
    con.executemany("INSERT OR REPLACE INTO outdoor VALUES (?,?,?,?)", rows)
    for k, v in {"station": station_label, **(extra_meta or {})}.items():
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, str(v)))
    con.commit()
    after = con.execute("SELECT COUNT(*) FROM outdoor").fetchone()[0]
    con.close()
    return after - before


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------
def _get(url: str, binary: bool = False, timeout: int = 60):
    import requests
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content if binary else resp.content.decode("latin-1")


def download_station(station: dict, since: str) -> pd.DataFrame:
    sid = station["id"]
    urls = []
    index = _get(f"{BASE}/historical/")
    for name in sorted(set(re.findall(rf"stundenwerte_TU_{sid}_\d{{8}}_\d{{8}}_hist\.zip", index))):
        urls.append(f"{BASE}/historical/{name}")
    urls.append(f"{BASE}/recent/stundenwerte_TU_{sid}_akt.zip")
    frames = []
    for url in urls:
        print(f"  downloading {url.rsplit('/', 1)[1]} …", flush=True)
        with zipfile.ZipFile(io.BytesIO(_get(url, binary=True, timeout=180))) as zf:
            name = next(n for n in zf.namelist() if n.lower().startswith("produkt_tu_stunde"))
            frames.append(parse_product(zf.read(name)))
    df = pd.concat(frames).drop_duplicates("timestamp").sort_values("timestamp")
    return df[df["timestamp"] >= pd.Timestamp(since)]


def main() -> int:
    ap = argparse.ArgumentParser(description="Outdoor air temperature (DWD) into data/outdoor.db")
    ap.add_argument("--station", default=DEFAULT_STATION, help=f"Station name (default: {DEFAULT_STATION})")
    ap.add_argument("--since", default="2020-11-01", help="Keep data from this date (default: start of the CO2 data)")
    ap.add_argument("--file", type=Path, help="Use a DWD zip/txt you downloaded instead of the internet")
    args = ap.parse_args()

    if args.file:
        df = read_zip_or_txt(args.file)
        added = store(df, args.station, {"source_file": args.file.name})
        print(f"Read {len(df)} hourly values from {args.file.name}; {added} new. Range {df['timestamp'].min()} → {df['timestamp'].max()}")
        return 0
    try:
        stations = parse_station_list(_get(f"{BASE}/recent/TU_Stundenwerte_Beschreibung_Stationen.txt"))
    except Exception as exc:
        print(f"Could not reach DWD open data: {exc}\nDownload the station zip manually and use --file (see --help).")
        return 1
    station = find_station(stations, args.station)
    if not station:
        names = ", ".join(sorted(s["name"] for s in stations if "stuttgart" in s["name"].lower()))
        print(f"No station matching '{args.station}'. Stuttgart stations: {names}")
        return 1
    print(f"Station {station['id']}: {station['name']} ({station['height_m']} m, {station['lat']}, {station['lon']})")
    df = download_station(station, args.since)
    label = f"DWD {station['name']} ({station['id']})"
    added = store(df, label, {"station_id": station["id"], "height_m": station["height_m"]})
    print(f"{len(df)} hourly values, {added} new. Range {df['timestamp'].min()} → {df['timestamp'].max()}")
    print(f"Written: {DB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
