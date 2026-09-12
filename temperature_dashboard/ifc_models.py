from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import shutil
import sqlite3
import uuid
import zipfile
from contextlib import contextmanager
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from temperature_dashboard.db import DATA_DIR, canonical_building, canonical_floor, canonical_room

MODEL_DIR = DATA_DIR / "ifc_models"
REGISTRY_PATH = DATA_DIR / "ifc_models.db"
MODEL_DIR.mkdir(parents=True, exist_ok=True)
ROLES = ("Architecture", "HVAC", "Structure", "Electrical", "Section", "Other")
MAX_UPLOAD_BYTES = 500 * 1024 * 1024
MAX_FLOOR_PLAN_BYTES = 50 * 1024 * 1024


@contextmanager
def _connect():
    con = sqlite3.connect(REGISTRY_PATH, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS models (
            id INTEGER PRIMARY KEY, building TEXT NOT NULL, filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL, role TEXT NOT NULL, revision TEXT NOT NULL DEFAULT '',
            sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, schema_name TEXT,
            project_name TEXT, ifc_building_names TEXT, uploaded_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_models_building_active ON models(building, active);
        CREATE TABLE IF NOT EXISTS spaces (
            model_id INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
            global_id TEXT NOT NULL, ifc_name TEXT, long_name TEXT, storey_name TEXT,
            storey_elevation REAL, geometry_file TEXT, vertex_count INTEGER NOT NULL DEFAULT 0,
            dashboard_building TEXT, dashboard_floor TEXT, dashboard_room TEXT,
            mapping_source TEXT NOT NULL DEFAULT 'automatic',
            PRIMARY KEY(model_id, global_id)
        );
        CREATE TABLE IF NOT EXISTS floor_plans (
            building TEXT NOT NULL,
            floor TEXT NOT NULL,
            filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            uploaded_at TEXT NOT NULL,
            PRIMARY KEY(building, floor)
        );
        CREATE INDEX IF NOT EXISTS idx_floor_plans_building ON floor_plans(building);
    """)
    try:
        yield con
    finally:
        con.close()


def initialize_registry() -> None:
    with _connect() as con:
        rows = con.execute("""SELECT model_id, global_id, ifc_name, long_name, storey_name
                              FROM spaces WHERE mapping_source='automatic'""").fetchall()
        updates = []
        for row in rows:
            floor, room = _automatic_location(row["ifc_name"], row["long_name"], row["storey_name"])
            updates.append((floor, room, row["model_id"], row["global_id"]))
        con.executemany("""UPDATE spaces SET dashboard_floor=?, dashboard_room=?
                           WHERE model_id=? AND global_id=? AND mapping_source='automatic'""", updates)
        con.commit()


def _ifc_payload(filename: str, payload: bytes) -> tuple[str, bytes]:
    if len(payload) > MAX_UPLOAD_BYTES:
        raise ValueError("The upload exceeds the 500 MB per-file limit.")
    suffix = Path(filename).suffix.lower()
    if suffix == ".ifc":
        if not payload.lstrip().startswith(b"ISO-10303-21"):
            raise ValueError("The file does not have a valid IFC STEP header.")
        return Path(filename).name, payload
    if suffix not in {".ifczip", ".zip"}:
        raise ValueError("Only .ifc and .ifczip files are supported.")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            candidates = [n for n in archive.namelist() if not n.endswith("/") and Path(n).suffix.lower() == ".ifc"]
            if len(candidates) != 1:
                raise ValueError("An IFCZIP must contain exactly one IFC file.")
            info = archive.getinfo(candidates[0])
            if info.file_size > MAX_UPLOAD_BYTES:
                raise ValueError("The IFC inside the archive exceeds 500 MB.")
            data = archive.read(info)
    except zipfile.BadZipFile as exc:
        raise ValueError("The IFCZIP archive is invalid.") from exc
    if not data.lstrip().startswith(b"ISO-10303-21"):
        raise ValueError("The archived file does not have a valid IFC STEP header.")
    return Path(candidates[0]).name, data


def _label(entity, attribute: str = "Name") -> str:
    return str(getattr(entity, attribute, "") or "").strip()


def _automatic_location(ifc_name: str, long_name: str, storey_name: str) -> tuple[str, str]:
    """Prefer an HFT numeric space Name over a descriptive IFC LongName."""
    name = str(ifc_name or "").strip()
    long = str(long_name or "").strip()
    room_source = name if re.fullmatch(r"\d+(?:\.0+)?", name) else (long or name)
    room = canonical_room(room_source)
    return canonical_floor(storey_name, room), room


def _storey_for(space):
    import ifcopenshell.util.element
    container = ifcopenshell.util.element.get_container(space)
    if container is None:
        container = ifcopenshell.util.element.get_aggregate(space)
    while container is not None and not container.is_a("IfcBuildingStorey"):
        container = (ifcopenshell.util.element.get_container(container)
                     or ifcopenshell.util.element.get_aggregate(container))
    return container


def _extract(ifc_path: Path, geometry_path: Path) -> tuple[dict, list[dict]]:
    import ifcopenshell
    import ifcopenshell.geom
    import ifcopenshell.util.placement

    model = ifcopenshell.open(str(ifc_path))
    projects, buildings = model.by_type("IfcProject"), model.by_type("IfcBuilding")
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    geometries: dict[str, dict] = {}
    rows: list[dict] = []
    for space in model.by_type("IfcSpace"):
        guid = str(space.GlobalId)
        storey = _storey_for(space)
        storey_name = _label(storey) if storey else "Unassigned"
        elevation = getattr(storey, "Elevation", None) if storey else None
        if elevation is None and storey and getattr(storey, "ObjectPlacement", None):
            try:
                elevation = float(ifcopenshell.util.placement.get_local_placement(storey.ObjectPlacement)[2, 3])
            except Exception:
                elevation = None
        geometry = None
        try:
            shape = ifcopenshell.geom.create_shape(settings, space)
            vertices = [float(v) for v in shape.geometry.verts]
            faces = [int(v) for v in shape.geometry.faces]
            if vertices and faces:
                geometry = {"vertices": vertices, "faces": faces}
                geometries[guid] = geometry
        except Exception:
            pass
        ifc_name, long_name = _label(space), _label(space, "LongName")
        floor_guess, room_guess = _automatic_location(ifc_name, long_name, storey_name)
        rows.append({"global_id": guid, "ifc_name": ifc_name, "long_name": long_name,
                     "storey_name": storey_name, "storey_elevation": elevation,
                     "vertex_count": len(geometry["vertices"]) // 3 if geometry else 0,
                     "dashboard_floor": floor_guess, "dashboard_room": room_guess})
    with gzip.open(geometry_path, "wt", encoding="utf-8") as handle:
        json.dump(geometries, handle, separators=(",", ":"))
    return {"schema_name": str(model.schema), "project_name": _label(projects[0]) if projects else "",
            "ifc_building_names": ", ".join(filter(None, (_label(item) for item in buildings)))}, rows



def ingest_model(filename: str, payload: bytes, building: str, role: str, revision: str = "", replace_role: bool = False) -> dict:
    initialize_registry()
    building = canonical_building(building)
    if building == "Unassigned":
        raise ValueError("Assign the model to a dashboard building.")
    if role not in ROLES:
        raise ValueError(f"Unknown model role: {role}")
    _, ifc_data = _ifc_payload(filename, payload)
    digest = hashlib.sha256(ifc_data).hexdigest()
    with _connect() as con:
        duplicate = con.execute("SELECT id FROM models WHERE building=? AND sha256=?", (building, digest)).fetchone()
        if duplicate:
            raise ValueError(f"This IFC is already registered as model #{duplicate['id']} for {building}.")
        cursor = con.execute(
            """INSERT INTO models(building, filename, stored_filename, role, revision, sha256, size_bytes, uploaded_at, active)
               VALUES(?, ?, '', ?, ?, ?, ?, ?, 0)""",
            (building, Path(filename).name, role, revision.strip(), digest, len(ifc_data), datetime.now(timezone.utc).isoformat()),
        )
        model_id = int(cursor.lastrowid)
        con.commit()
    folder = MODEL_DIR / str(model_id)
    folder.mkdir(parents=True, exist_ok=False)
    ifc_path, geometry_path = folder / "model.ifc", folder / "spaces.json.gz"
    try:
        ifc_path.write_bytes(ifc_data)
        metadata, spaces = _extract(ifc_path, geometry_path)
        with _connect() as con:
            if replace_role:
                con.execute("UPDATE models SET active=0 WHERE building=? AND role=? AND id<>?", (building, role, model_id))
            con.execute("""UPDATE models SET stored_filename=?, schema_name=?, project_name=?, ifc_building_names=?, active=1
                           WHERE id=?""", (str(ifc_path.relative_to(DATA_DIR)), metadata["schema_name"], metadata["project_name"],
                                          metadata["ifc_building_names"], model_id))
            con.executemany(
                """INSERT INTO spaces(model_id, global_id, ifc_name, long_name, storey_name, storey_elevation,
                   geometry_file, vertex_count, dashboard_building, dashboard_floor, dashboard_room, mapping_source)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'automatic')""",
                [(model_id, row["global_id"], row["ifc_name"], row["long_name"], row["storey_name"], row["storey_elevation"],
                  str(geometry_path.relative_to(DATA_DIR)), row["vertex_count"], building, row["dashboard_floor"], row["dashboard_room"])
                 for row in spaces],
            )
            con.commit()
        return {"id": model_id, "spaces": len(spaces), "geometry_spaces": sum(row["vertex_count"] > 0 for row in spaces), **metadata}
    except Exception as exc:
        with _connect() as con:
            con.execute("UPDATE models SET error=?, active=0 WHERE id=?", (str(exc), model_id))
            con.commit()
        raise


def list_models(building: str | None = None, active_only: bool = False) -> pd.DataFrame:
    initialize_registry()
    clauses, params = [], []
    if building is not None:
        clauses.append("m.building=?")
        params.append(building)
    if active_only:
        clauses.append("m.active=1")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    sql = f"""SELECT m.id, m.building, m.filename, m.role, m.revision, m.schema_name, m.project_name,
              m.ifc_building_names, m.size_bytes, m.uploaded_at, m.active, m.error,
              COUNT(s.global_id) AS spaces, COALESCE(SUM(CASE WHEN s.vertex_count>0 THEN 1 ELSE 0 END), 0) AS geometry_spaces
              FROM models m LEFT JOIN spaces s ON s.model_id=m.id {where} GROUP BY m.id ORDER BY m.uploaded_at DESC"""
    with _connect() as con:
        return pd.read_sql_query(sql, con, params=params)


def set_model_active(model_id: int, active: bool) -> None:
    with _connect() as con:
        con.execute("UPDATE models SET active=? WHERE id=?", (int(active), int(model_id)))
        con.commit()


def delete_model(model_id: int) -> None:
    with _connect() as con:
        row = con.execute("SELECT id FROM models WHERE id=?", (int(model_id),)).fetchone()
        if not row:
            return
        con.execute("DELETE FROM models WHERE id=?", (int(model_id),))
        con.commit()
    shutil.rmtree(MODEL_DIR / str(int(model_id)), ignore_errors=True)


def _floor_plan_path(stored_filename: str) -> Path:
    return MODEL_DIR / "floor_plans" / Path(stored_filename).name


def _floor_plan_payload(filename: str, payload: bytes) -> tuple[str, bytes]:
    safe_name = Path(filename or "floor-plan.pdf").name
    if Path(safe_name).suffix.lower() != ".pdf":
        raise ValueError("Only PDF floor plans are supported.")
    if not payload.startswith(b"%PDF-"):
        raise ValueError("The uploaded floor plan is not a valid PDF file.")
    if len(payload) > MAX_FLOOR_PLAN_BYTES:
        raise ValueError("The floor-plan PDF exceeds the 50 MB upload limit.")
    return safe_name, payload


def save_floor_plan(filename: str, payload: bytes, building: str, floor: str) -> dict:
    """Store one replacement-safe PDF plan for a dashboard building and floor."""
    initialize_registry()
    safe_name, pdf_data = _floor_plan_payload(filename, payload)
    building = canonical_building(building)
    floor = canonical_floor(floor)
    if building == "Unassigned" or floor == "Unassigned":
        raise ValueError("Choose a dashboard building and floor before uploading a plan.")

    digest = hashlib.sha256(pdf_data).hexdigest()
    stored_filename = f"{digest}-{uuid.uuid4().hex}.pdf"
    target_path = _floor_plan_path(stored_filename)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = target_path.with_suffix(".uploading")
    temporary_path.write_bytes(pdf_data)

    replaced_filename = None
    try:
        with _connect() as con:
            existing = con.execute(
                "SELECT stored_filename FROM floor_plans WHERE building=? AND floor=?",
                (building, floor),
            ).fetchone()
            replaced_filename = existing["stored_filename"] if existing else None
            con.execute(
                """INSERT INTO floor_plans(building, floor, filename, stored_filename, sha256, size_bytes, uploaded_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(building, floor) DO UPDATE SET
                    filename=excluded.filename,
                    stored_filename=excluded.stored_filename,
                    sha256=excluded.sha256,
                    size_bytes=excluded.size_bytes,
                    uploaded_at=excluded.uploaded_at""",
                (building, floor, safe_name, stored_filename, digest, len(pdf_data), datetime.now(timezone.utc).isoformat()),
            )
            con.commit()
            temporary_path.replace(target_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        target_path.unlink(missing_ok=True)
        raise

    if replaced_filename and replaced_filename != stored_filename:
        _floor_plan_path(replaced_filename).unlink(missing_ok=True)
    return {"building": building, "floor": floor, "filename": safe_name, "size_bytes": len(pdf_data)}


def list_floor_plans(building: str | None = None) -> pd.DataFrame:
    initialize_registry()
    where, params = ("WHERE building=?", [canonical_building(building)]) if building else ("", [])
    with _connect() as con:
        return pd.read_sql_query(
            f"SELECT building, floor, filename, stored_filename, size_bytes, uploaded_at FROM floor_plans {where} ORDER BY building, floor",
            con,
            params=params,
        )


def floor_plan(building: str, floor: str) -> dict | None:
    initialize_registry()
    building, floor = canonical_building(building), canonical_floor(floor)
    with _connect() as con:
        row = con.execute(
            "SELECT building, floor, filename, stored_filename, size_bytes, uploaded_at FROM floor_plans WHERE building=? AND floor=?",
            (building, floor),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    path = _floor_plan_path(item["stored_filename"])
    if not path.exists():
        return None
    item["path"] = path
    return item


def delete_floor_plan(building: str, floor: str) -> bool:
    initialize_registry()
    building, floor = canonical_building(building), canonical_floor(floor)
    with _connect() as con:
        row = con.execute(
            "SELECT stored_filename FROM floor_plans WHERE building=? AND floor=?",
            (building, floor),
        ).fetchone()
        if row is None:
            return False
        con.execute("DELETE FROM floor_plans WHERE building=? AND floor=?", (building, floor))
        remaining = con.execute(
            "SELECT COUNT(*) FROM floor_plans WHERE stored_filename=?",
            (row["stored_filename"],),
        ).fetchone()[0]
        con.commit()
    if not remaining:
        _floor_plan_path(row["stored_filename"]).unlink(missing_ok=True)
    return True


def space_mappings(building: str | None = None) -> pd.DataFrame:
    where, params = (" WHERE m.building=?", [building]) if building else ("", [])
    sql = f"""SELECT s.model_id, m.filename, m.role, s.global_id, s.ifc_name, s.long_name, s.storey_name,
              s.vertex_count, s.dashboard_building, s.dashboard_floor, s.dashboard_room, s.mapping_source
              FROM spaces s JOIN models m ON m.id=s.model_id {where}
              ORDER BY m.id, s.storey_elevation, s.storey_name, s.ifc_name"""
    with _connect() as con:
        return pd.read_sql_query(sql, con, params=params)


def update_space_mappings(frame: pd.DataFrame) -> int:
    required = {"model_id", "global_id", "dashboard_building", "dashboard_floor", "dashboard_room"}
    if not required.issubset(frame.columns):
        raise ValueError("The mapping table is missing required columns.")
    updates = []
    for row in frame.itertuples(index=False):
        updates.append((canonical_building(row.dashboard_building), canonical_floor(row.dashboard_floor, row.dashboard_room),
                        canonical_room(row.dashboard_room), "manual", int(row.model_id), str(row.global_id)))
    with _connect() as con:
        con.executemany("""UPDATE spaces SET dashboard_building=?, dashboard_floor=?, dashboard_room=?, mapping_source=?
                           WHERE model_id=? AND global_id=?""", updates)
        con.commit()
    return len(updates)



@lru_cache(maxsize=16)
def _geometry_payload(relative_path: str, modified_ns: int) -> dict:
    """Decode each extracted IFC geometry file once per process and file version."""
    del modified_ns  # Included in the cache key to invalidate replaced files.
    with gzip.open(DATA_DIR / relative_path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def clear_geometry_cache() -> None:
    _geometry_payload.cache_clear()


def viewer_spaces(building: str, floor: str | None = None, room: str | None = None) -> list[dict]:
    clauses = ["m.active=1", "m.building=?", "s.dashboard_building=?", "s.vertex_count>0"]
    params: list = [building, building]
    if floor is not None:
        clauses.append("s.dashboard_floor=?")
        params.append(floor)
    if room is not None:
        clauses.append("s.dashboard_room=?")
        params.append(room)
    with _connect() as con:
        rows = con.execute(f"""SELECT s.*, m.filename, m.role, m.revision FROM spaces s JOIN models m ON m.id=s.model_id
                               WHERE {' AND '.join(clauses)} ORDER BY s.storey_elevation, s.storey_name, s.ifc_name""", params).fetchall()
    cache: dict[str, dict] = {}
    output = []
    for row in rows:
        item = dict(row)
        geometry_file = item["geometry_file"]
        if geometry_file not in cache:
            path = DATA_DIR / geometry_file
            cache[geometry_file] = _geometry_payload(geometry_file, path.stat().st_mtime_ns)
        geometry = cache[geometry_file].get(item["global_id"])
        if geometry:
            item["geometry"] = geometry
            output.append(item)
    return output


def model_buildings() -> list[str]:
    initialize_registry()
    with _connect() as con:
        return [row[0] for row in con.execute("SELECT DISTINCT building FROM models WHERE active=1 ORDER BY building")]


initialize_registry()
