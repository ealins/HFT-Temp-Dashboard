"""Extract a light per-room envelope (external walls, windows, roof/ground) from the IFC.

Run once per building in the terminal (reads the IFC a single time, ~1 min, low memory):

    python scripts\\extract_room_envelope.py --building 1

Writes data/ifc_models/<building>/envelope.json (a few hundred kB). The Room Analysis
view reads that file; it never opens the IFC.

What comes from the IFC: room floor area and height, which room edges face the outside,
window/door areas and their orientation. What does NOT: U-values. The IFC only carries
Revit defaults (a 0.15 m concrete wall shows U = 6.97), so U-values are assumptions that
you can edit in the view.
"""
from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

PROBE_M = 0.7          # how far outside a room edge we look for another room / the footprint
WINDOW_REACH_M = 0.8   # a window belongs to a room edge if its centre is this close
MIN_EDGE_M = 0.25      # ignore raster staircase steps shorter than this


def _point_in_ring(pt, ring) -> bool:
    x, y = pt
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _classify(pt, fp) -> str:
    """'inside' the building, 'outside' it, or in a 'hole' (courtyard / atrium)."""
    if not _point_in_ring(pt, fp["outer"]):
        return "outside"
    return "hole" if any(_point_in_ring(pt, h) for h in fp["holes"]) else "inside"


def _dist_to_segment(p, a, b) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _compass(nx: float, ny: float, north_offset: float) -> str:
    bearing = (math.degrees(math.atan2(nx, ny)) + north_offset) % 360   # clockwise from model +Y
    return ("N", "E", "S", "W")[int(((bearing + 45) % 360) // 90)]


def read_openings(ifc_path: Path) -> list[dict]:
    """Windows and doors with world-coordinate extents (geometry only, ~10 s)."""
    import ifcopenshell
    import ifcopenshell.geom
    import numpy as np

    model = ifcopenshell.open(str(ifc_path))
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    elements = model.by_type("IfcWindow") + model.by_type("IfcDoor")
    out = []
    iterator = ifcopenshell.geom.iterator(settings, model, max(1, multiprocessing.cpu_count() // 2), include=elements)
    if iterator.initialize():
        while True:
            shape = iterator.get()
            element = model.by_id(shape.id)
            pts = np.array(shape.geometry.verts, dtype=float).reshape(-1, 3)
            if len(pts):
                out.append({"kind": "window" if element.is_a("IfcWindow") else "door",
                            "xy": pts[:, :2].round(3).tolist(), "zmin": float(pts[:, 2].min()),
                            "zmax": float(pts[:, 2].max()), "name": str(element.Name or "")[:60]})
            if not iterator.next():
                break
    return out


def build_envelope(spaces: list[dict], openings: list[dict], north_offset: float = 0.0) -> dict:
    from run_simstadt_light import _is_above_ground, trace_footprint

    # footprint of every storey (what counts as "outside")
    storeys: dict[str, list[dict]] = defaultdict(list)
    for s in spaces:
        storeys[s.get("storey_name") or "?"].append(s)
    footprints = {}
    for name, rooms in storeys.items():
        try:
            footprints[name] = trace_footprint(rooms, cell=0.25, gap=4.0, min_courtyard=20.0, wall=0.3)
        except Exception:
            footprints[name] = None
    above = sorted({s["storey_name"]: float(s.get("storey_elevation") or 0) for s in spaces if _is_above_ground(s)}.items(),
                   key=lambda kv: kv[1])
    top_storey = above[-1][0] if above else None
    ground_storey = above[0][0] if above else None
    has_basement = any(not _is_above_ground(s) for s in spaces)

    # per-space edges
    records = []
    for s in spaces:
        geo = s.get("geometry") or {}
        v, f = geo.get("vertices") or [], geo.get("faces") or []
        if not v or not f:
            continue
        zs = v[2::3]
        zmin, zmax = min(zs), max(zs)
        try:
            ring = trace_footprint([s], cell=0.1, gap=0.3, min_courtyard=1.0, wall=0.0)
        except Exception:
            continue
        outer = ring["outer"]
        fp = footprints.get(s.get("storey_name") or "?")
        edges = []
        n = len(outer)
        for i in range(n):
            a, b = outer[i], outer[(i + 1) % n]
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            if length < MIN_EDGE_M:
                continue
            nx, ny = (b[1] - a[1]) / length, -(b[0] - a[0]) / length   # outward for a CCW ring
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            probe = (mid[0] + PROBE_M * nx, mid[1] + PROBE_M * ny)
            kind = _classify(probe, fp) if fp else "inside"
            edges.append({"a": a, "b": b, "len": length, "n": (nx, ny), "kind": kind,
                          "win": 0.0, "door": 0.0, "evidence": "geometry" if kind != "inside" else ""})
        records.append({"space": s, "area": ring["footprint_m2"], "zmin": zmin, "zmax": zmax,
                        "height": max(zmax - zmin, 2.0), "edges": edges})

    # attach windows / doors to the closest room edge
    unmatched = 0
    for op in openings:
        pts = op["xy"]
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        zc = (op["zmin"] + op["zmax"]) / 2
        best = None
        for rec in records:
            if not (rec["zmin"] - 0.2 <= zc <= rec["zmax"] + 0.2):
                continue
            for edge in rec["edges"]:
                d = _dist_to_segment((cx, cy), edge["a"], edge["b"])
                if d <= WINDOW_REACH_M and (best is None or d < best[0]):
                    best = (d, rec, edge)
        if not best:
            unmatched += 1
            continue
        _, rec, edge = best
        tx, ty = (edge["b"][0] - edge["a"][0]) / edge["len"], (edge["b"][1] - edge["a"][1]) / edge["len"]
        proj = [p[0] * tx + p[1] * ty for p in pts]
        width = max(proj) - min(proj)
        area = width * (op["zmax"] - op["zmin"])
        if op["kind"] == "window":
            edge["win"] += area           # a window on an "inside" edge proves it faces air (corridor gap)
            if edge["kind"] == "inside":
                edge["kind"], edge["evidence"] = "outside", "window"
        else:
            edge["door"] += area

    # aggregate per dashboard room
    rooms: dict[str, dict] = {}
    for rec in records:
        s = rec["space"]
        floor, room = s.get("dashboard_floor"), s.get("dashboard_room")
        if not floor or not room:
            continue
        key = f"{floor}|{room}"
        entry = rooms.setdefault(key, {
            "floor": floor, "room": room, "storey": s.get("storey_name"), "ifc_spaces": 0,
            "area_m2": 0.0, "volume_m3": 0.0, "height_m": 0.0, "external_edges": 0,
            "wall_gross_m2": {}, "window_m2": {}, "door_m2": 0.0, "wall_net_m2": 0.0,
            "roof_m2": 0.0, "ground_m2": 0.0, "ground_contact_walls": False, "evidence": [],
            "atrium_wall_net_m2": 0.0, "atrium_window_m2": 0.0})
        entry["ifc_spaces"] += 1
        entry["area_m2"] += rec["area"]
        entry["volume_m3"] += rec["area"] * rec["height"]
        entry["height_m"] = max(entry["height_m"], rec["height"])
        below = not _is_above_ground(s)
        for edge in rec["edges"]:
            if edge["kind"] == "hole":      # faces a courtyard / atrium, not the outside
                entry["atrium_wall_net_m2"] += max(edge["len"] * rec["height"] - edge["win"] - edge["door"], 0.0)
                entry["atrium_window_m2"] += edge["win"]
                continue
            if edge["kind"] != "outside":
                continue
            side = _compass(edge["n"][0], edge["n"][1], north_offset)
            gross = edge["len"] * rec["height"]
            entry["external_edges"] += 1
            entry["wall_gross_m2"][side] = entry["wall_gross_m2"].get(side, 0.0) + gross
            entry["window_m2"][side] = entry["window_m2"].get(side, 0.0) + edge["win"]
            entry["door_m2"] += edge["door"]
            entry["wall_net_m2"] += max(gross - edge["win"] - edge["door"], 0.0)
            if edge["evidence"] not in entry["evidence"]:
                entry["evidence"].append(edge["evidence"])
            entry["ground_contact_walls"] = entry["ground_contact_walls"] or below
        if s.get("storey_name") == top_storey:
            entry["roof_m2"] += rec["area"]
        if s.get("storey_name") == ground_storey:
            entry["ground_m2"] += rec["area"]
    for entry in rooms.values():
        for k in ("area_m2", "volume_m3", "height_m", "door_m2", "wall_net_m2", "roof_m2", "ground_m2",
                  "atrium_wall_net_m2", "atrium_window_m2"):
            entry[k] = round(entry[k], 2)
        entry["wall_gross_m2"] = {k: round(v, 2) for k, v in entry["wall_gross_m2"].items()}
        entry["window_m2"] = {k: round(v, 2) for k, v in entry["window_m2"].items()}
    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "rooms": rooms,
        "meta": {"openings_read": len(openings), "openings_unmatched_or_internal": unmatched,
                 "top_storey": top_storey, "ground_storey": ground_storey, "has_basement": has_basement,
                 "north_offset_deg": north_offset,
                 "notes": [
                     "External edge = room edge facing outside the storey footprint (voids under 8 m, e.g. corridors, are "
                     "filled) or carrying a window. Edges facing an enclosed courtyard/atrium are listed separately.",
                     "Window area = width x height of the IFC window geometry (frame included).",
                     "Model +Y is assumed to be north unless --north-offset is given.",
                     "U-values are NOT taken from the IFC (Revit defaults only); set them in the view.",
                 ]},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-room envelope from the IFC (run once per building).")
    ap.add_argument("--building", help="Default: building selected in the dashboard")
    ap.add_argument("--ifc", type=Path, help="IFC file (default: data/ifc_models/<building>/model.ifc)")
    ap.add_argument("--north-offset", type=float, default=0.0,
                    help="Degrees to add to compass bearings if model +Y is not north (default 0)")
    args = ap.parse_args()

    from temperature_dashboard.ifc_models import DATA_DIR, viewer_spaces

    building = args.building
    if not building:
        try:
            building = json.loads((PROJECT_ROOT / "simstadt_sandbox" / "outputs" / "current_selection.json")
                                  .read_text(encoding="utf-8"))["building"]
        except Exception:
            print("Pass --building.")
            return 1
    ifc = args.ifc or DATA_DIR / "ifc_models" / str(building) / "model.ifc"
    if not ifc.exists():
        print(f"IFC not found: {ifc}. Pass --ifc.")
        return 1
    t0 = time.monotonic()
    print(f"Reading rooms of building {building}…", flush=True)
    spaces = viewer_spaces(str(building))
    print(f"  {len(spaces)} rooms with geometry. Reading windows and doors from {ifc.name}…", flush=True)
    openings = read_openings(ifc)
    print(f"  {len(openings)} openings read ({time.monotonic() - t0:.0f} s). Matching to rooms…", flush=True)
    result = build_envelope(spaces, openings, args.north_offset)
    out = DATA_DIR / "ifc_models" / str(building) / "envelope.json"
    out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    rooms = result["rooms"]
    ext = sum(1 for r in rooms.values() if r["external_edges"])
    print(f"Done in {time.monotonic() - t0:.0f} s: {len(rooms)} rooms, {ext} with external walls, "
          f"{sum(sum(r['window_m2'].values()) for r in rooms.values()):.0f} m² of windows matched.")
    print(f"Written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
