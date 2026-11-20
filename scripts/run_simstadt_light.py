#!/usr/bin/env python
"""Light SimStadt run for the HFT dashboard (run in a terminal, not in Streamlit).

What it does
  1. Builds a small LoD2 block model of the building from the IFC room geometry the
     dashboard already stores (footprint traced from the rooms, one flat roof).
     No IFC -> CityGML conversion, so it needs almost no memory.
  2. Runs light SimStadt applications on that block through the installed
     ``simstadt`` Python package:
        heat    monthly heat demand (DIN V 18599 monthly balance, no shadow step)
        pv      rooftop photovoltaic potential
        refurb  heat demand again with full GEG refurbishment (savings scenario)
  3. Writes  simstadt_sandbox/outputs/<building>/light/results.json , which the
     dashboard's "SimStadt Sandbox" view displays.

Each application runs in its own child process at below-normal priority with a
time limit, so a stuck run cannot freeze the PC or the dashboard.

Examples (from the project folder, with the .venv active):
    python scripts\\run_simstadt_light.py                 # building from the dashboard, heat + pv
    python scripts\\run_simstadt_light.py --building 1 --apps heat,pv,refurb --year 1960
    python scripts\\run_simstadt_light.py --box-only      # only build and describe the block model
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

OUTPUTS = PROJECT_ROOT / "simstadt_sandbox" / "outputs"
SELECTION_FILE = OUTPUTS / "current_selection.json"
HFT = {"epsg": "EPSG:25832", "easting": 512723.256, "northing": 5403043.705, "ground_z": 247.0}

APPS = {
    "heat": "Heat demand (monthly balance)",
    "pv": "Rooftop PV potential",
    "refurb": "Heat demand after full GEG refurbishment",
}
_T0 = time.monotonic()


def step(msg: str) -> None:
    print(f"[{time.monotonic() - _T0:6.1f}s] {msg}", flush=True)


def light_dir(building: str) -> Path:
    return OUTPUTS / str(building) / "light"


def _clean(value):
    """Make numpy / NaN values JSON-safe."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except Exception:
            return str(value)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_clean(payload), indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Block model: footprint traced from the IFC rooms
# ---------------------------------------------------------------------------
def _is_above_ground(space: dict) -> bool:
    name = str(space.get("storey_name") or "").upper()
    if "UG" in name or name.startswith("-") or "KELLER" in name or "BASEMENT" in name:
        return False
    elevation = space.get("storey_elevation")
    return elevation is None or float(elevation) > -0.5


def _dominant_angle(edges: list[tuple[float, float]]) -> float:
    """Main wall direction (radians, period 90 degrees) from length-weighted edges."""
    c = s = 0.0
    for dx, dy in edges:
        length = math.hypot(dx, dy)
        if length < 0.3:
            continue
        angle4 = 4.0 * math.atan2(dy, dx)
        c += length * math.cos(angle4)
        s += length * math.sin(angle4)
    return math.atan2(s, c) / 4.0 if (c or s) else 0.0


def _grow(cells: set, r: int) -> set:
    offsets = [(dx, dy) for dx in range(-r, r + 1) for dy in range(-r, r + 1)]
    return {(x + dx, y + dy) for x, y in cells for dx, dy in offsets}


def _shrink(cells: set, r: int) -> set:
    offsets = [(dx, dy) for dx in range(-r, r + 1) for dy in range(-r, r + 1)]
    return {(x, y) for x, y in cells if all((x + dx, y + dy) in cells for dx, dy in offsets)}


def _components(cells: set) -> list[set]:
    todo, out = set(cells), []
    while todo:
        seed = todo.pop()
        comp, queue = {seed}, deque([seed])
        while queue:
            x, y = queue.popleft()
            for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if n in todo:
                    todo.remove(n)
                    comp.add(n)
                    queue.append(n)
        out.append(comp)
    return out


def _fill_small_holes(cells: set, max_hole_cells: int) -> set:
    xs = [c[0] for c in cells]
    ys = [c[1] for c in cells]
    x0, x1, y0, y1 = min(xs) - 1, max(xs) + 1, min(ys) - 1, max(ys) + 1
    empty = {(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)} - cells
    filled = set(cells)
    for comp in _components(empty):
        touches_border = any(x in (x0, x1) or y in (y0, y1) for x, y in comp)
        if not touches_border and len(comp) <= max_hole_cells:
            filled |= comp
    return filled


def _trace_loops(cells: set) -> list[list[tuple[int, int]]]:
    """Boundary loops of a cell set. Outer loops are counter-clockwise, holes clockwise."""
    nxt: dict = {}
    for x, y in cells:
        if (x, y - 1) not in cells:
            nxt.setdefault((x, y), []).append((x + 1, y))
        if (x + 1, y) not in cells:
            nxt.setdefault((x + 1, y), []).append((x + 1, y + 1))
        if (x, y + 1) not in cells:
            nxt.setdefault((x + 1, y + 1), []).append((x, y + 1))
        if (x - 1, y) not in cells:
            nxt.setdefault((x, y + 1), []).append((x, y))
    loops = []
    while nxt:
        start = next(iter(nxt))
        loop, cur, prev = [start], start, None
        while True:
            options = nxt[cur]
            if len(options) == 1 or prev is None:
                choice = options[0]
            else:  # two cells touch at a corner: turn left to keep loops separate
                din = (cur[0] - prev[0], cur[1] - prev[1])
                choice = max(options, key=lambda p: din[0] * (p[1] - cur[1]) - din[1] * (p[0] - cur[0]))
            options.remove(choice)
            if not options:
                del nxt[cur]
            prev, cur = cur, choice
            if cur == start:
                break
            loop.append(cur)
        # drop collinear points
        simple = []
        n = len(loop)
        for i in range(n):
            a, b, c = loop[i - 1], loop[i], loop[(i + 1) % n]
            if (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]) != 0:
                simple.append(b)
        if len(simple) >= 4:
            loops.append(simple)
    return loops


def _signed_area(ring) -> float:
    return 0.5 * sum(ring[i][0] * ring[(i + 1) % len(ring)][1] - ring[(i + 1) % len(ring)][0] * ring[i][1]
                     for i in range(len(ring)))


def trace_footprint(spaces: list[dict], cell: float = 0.5, gap: float = 1.5, min_courtyard: float = 150.0,
                    wall: float = 0.5) -> dict:
    """Footprint (outer ring + courtyards) and height from above-ground IFC rooms, in local metres."""
    above = [s for s in spaces if _is_above_ground(s)] or spaces
    below_storeys = {s.get("storey_name") for s in spaces if not _is_above_ground(s)}

    tris, edges, zs = [], [], []
    xs_all = []
    for space in above:
        geo = space.get("geometry") or {}
        v, f = geo.get("vertices") or [], geo.get("faces") or []
        xs_all += v[0::3]
        zs += v[2::3]
        for i in range(0, len(f) - 2, 3):
            pts = [(v[3 * k], v[3 * k + 1]) for k in f[i:i + 3]]
            tris.append(pts)
    if not tris:
        raise ValueError("The IFC rooms have no usable geometry.")
    scale = 0.001 if (max(xs_all) - min(xs_all)) > 2000 else 1.0  # millimetre models
    tris = [[(x * scale, y * scale) for x, y in t] for t in tris]
    for a, b, c in tris:
        edges += [(b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1]), (a[0] - c[0], a[1] - c[1])]
    theta = _dominant_angle(edges)
    cos_t, sin_t = math.cos(-theta), math.sin(-theta)

    def rot(p):
        return (p[0] * cos_t - p[1] * sin_t, p[0] * sin_t + p[1] * cos_t)

    cells: set = set()
    for tri in tris:
        (ax, ay), (bx, by), (cx, cy) = (rot(p) for p in tri)
        det = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(det) < 1e-4:  # vertical face: projects to a line
            continue
        i0, i1 = math.floor(min(ax, bx, cx) / cell), math.floor(max(ax, bx, cx) / cell)
        j0, j1 = math.floor(min(ay, by, cy) / cell), math.floor(max(ay, by, cy) / cell)
        for i in range(i0, i1 + 1):
            px = (i + 0.5) * cell
            for j in range(j0, j1 + 1):
                py = (j + 0.5) * cell
                l1 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / det
                l2 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / det
                if l1 >= -0.02 and l2 >= -0.02 and (1 - l1 - l2) >= -0.02:
                    cells.add((i, j))
    room_area = len(cells) * cell * cell
    if not cells:
        raise ValueError("Could not rasterise the IFC rooms into a footprint.")

    r = max(1, round(gap / cell))
    merged = _shrink(_grow(cells, r), r) | cells          # bridge walls and corridors
    merged = _fill_small_holes(merged, int(min_courtyard / (cell * cell)))
    merged = _grow(merged, max(1, round(wall / cell)))      # external wall thickness
    comps = sorted(_components(merged), key=len, reverse=True)
    main = comps[0]
    dropped_area = sum(len(c) for c in comps[1:]) * cell * cell

    loops = _trace_loops(main)
    cos_b, sin_b = math.cos(theta), math.sin(theta)

    def to_local(p):
        x, y = p[0] * cell, p[1] * cell
        return (x * cos_b - y * sin_b, x * sin_b + y * cos_b)

    rings = [[to_local(p) for p in loop] for loop in loops]
    outer = max(rings, key=lambda ring: _signed_area(ring))
    holes = [ring for ring in rings if ring is not outer and _signed_area(ring) < 0]
    footprint = _signed_area(outer) + sum(_signed_area(h) for h in holes)

    z_ground, z_top = min(zs) * scale, max(zs) * scale
    return {
        "outer": outer,
        "holes": holes,
        "height": max(z_top - z_ground, 3.0),
        "footprint_m2": footprint,
        "room_area_m2": room_area,
        "wall_faces": len(outer) + sum(len(h) for h in holes),
        "courtyards": len(holes),
        "storeys_above": max(len({s.get("storey_name") for s in above}), 1),
        "storeys_below": len(below_storeys),
        "rooms": len(spaces),
        "dropped_area_m2": dropped_area,
        "rotation_deg": math.degrees(theta),
    }


def _ring_xml(ring3d) -> str:
    closed = list(ring3d) + [ring3d[0]]
    coords = " ".join(f"{x:.3f} {y:.3f} {z:.3f}" for x, y, z in closed)
    return f'<gml:LinearRing><gml:posList srsDimension="3">{coords}</gml:posList></gml:LinearRing>'


def write_block_citygml(fp: dict, building: str, out_path: Path, year: int, function: str) -> Path:
    """Single-building CityGML 2.0 LoD2 block (ground, flat roof, walls) at the HFT location."""
    xs = [p[0] for p in fp["outer"]]
    ys = [p[1] for p in fp["outer"]]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2

    def place(ring):
        return [(HFT["easting"] + x - cx, HFT["northing"] + y - cy) for x, y in ring]

    outer, holes = place(fp["outer"]), [place(h) for h in fp["holes"]]
    z0, z1 = HFT["ground_z"], HFT["ground_z"] + fp["height"]
    bid = "HFT_Bau_" + "".join(ch if ch.isalnum() else "_" for ch in str(building))

    surfaces, refs = [], []

    def add(kind: str, sid: str, exterior, interiors=()):
        inner = "".join(f"<gml:interior>{_ring_xml(r)}</gml:interior>" for r in interiors)
        surfaces.append(
            f'<bldg:boundedBy><bldg:{kind} gml:id="{bid}_{sid}"><bldg:lod2MultiSurface><gml:MultiSurface>'
            f'<gml:surfaceMember><gml:Polygon gml:id="{bid}_{sid}_poly"><gml:exterior>{_ring_xml(exterior)}'
            f'</gml:exterior>{inner}</gml:Polygon></gml:surfaceMember></gml:MultiSurface>'
            f'</bldg:lod2MultiSurface></bldg:{kind}></bldg:boundedBy>')
        refs.append(f'<gml:surfaceMember xlink:href="#{bid}_{sid}_poly"/>')

    add("GroundSurface", "ground", [(x, y, z0) for x, y in reversed(outer)],
        [[(x, y, z0) for x, y in reversed(h)] for h in holes])
    add("RoofSurface", "roof", [(x, y, z1) for x, y in outer], [[(x, y, z1) for x, y in h] for h in holes])
    n = 0
    for ring in [outer] + holes:  # interior stays on the left, so every wall faces outwards
        for a, b in zip(ring, ring[1:] + ring[:1]):
            add("WallSurface", f"wall_{n}", [(a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], z1), (a[0], a[1], z1)])
            n += 1

    lx, ly, ux, uy = (min(p[0] for p in outer), min(p[1] for p in outer),
                      max(p[0] for p in outer), max(p[1] for p in outer))
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel xmlns:core="http://www.opengis.net/citygml/2.0" xmlns:bldg="http://www.opengis.net/citygml/building/2.0" xmlns:gml="http://www.opengis.net/gml" xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://www.opengis.net/citygml/building/2.0 http://schemas.opengis.net/citygml/building/2.0/building.xsd">
<gml:name>{bid} LoD2 block (light SimStadt input)</gml:name>
<gml:boundedBy><gml:Envelope srsName="{HFT['epsg']}" srsDimension="3"><gml:lowerCorner>{lx:.3f} {ly:.3f} {z0:.3f}</gml:lowerCorner><gml:upperCorner>{ux:.3f} {uy:.3f} {z1:.3f}</gml:upperCorner></gml:Envelope></gml:boundedBy>
<core:cityObjectMember><bldg:Building gml:id="{bid}">
<gml:name>{bid}</gml:name>
<bldg:function>{function}</bldg:function>
<bldg:yearOfConstruction>{year}</bldg:yearOfConstruction>
<bldg:roofType>1000</bldg:roofType>
<bldg:measuredHeight uom="m">{fp['height']:.2f}</bldg:measuredHeight>
<bldg:storeysAboveGround>{fp['storeys_above']}</bldg:storeysAboveGround>
<bldg:storeysBelowGround>{fp['storeys_below']}</bldg:storeysBelowGround>
<bldg:lod2Solid><gml:Solid gml:id="{bid}_solid"><gml:exterior><gml:CompositeSurface>
{chr(10).join(refs)}
</gml:CompositeSurface></gml:exterior></gml:Solid></bldg:lod2Solid>
{chr(10).join(surfaces)}
</bldg:Building></core:cityObjectMember>
</core:CityModel>
"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(xml, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# SimStadt
# ---------------------------------------------------------------------------
def preflight() -> tuple[bool, list[str], str]:
    """Check the SimStadt installation without starting it."""
    notes: list[str] = []
    try:
        import simstadt  # noqa: F401
        from simstadt.runner import get_simstadt_folder
    except Exception as exc:
        return False, [f"The 'simstadt' Python package is not usable ({exc}). Run scripts\\setup_simstadt.ps1."], ""
    try:
        folder = str(get_simstadt_folder())
    except ValueError as exc:
        return False, [str(exc).strip(),
                       "Fix: run   simstadt --install   (downloads SimStadt to your Desktop), or set "
                       "SIMSTADT_FOLDER in a .env file in the project folder."], ""
    java = shutil.which("java")
    if not java:
        notes.append("Java was not found on PATH. SimStadt needs Java 17; the run will fail if it has none bundled.")
    else:
        try:
            out = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=30).stderr
            import re
            m = re.search(r'version "(\d+)', out)
            if m and int(m.group(1)) < 17:
                notes.append(f"Java {m.group(1)} found; SimStadt needs Java 17 or newer.")
        except Exception:
            pass
    return True, notes, folder


def worker(app: str, gml: Path, project: Path, out: Path) -> int:
    """Child process: run one SimStadt application and write its result JSON."""
    import logging
    logging.basicConfig(level=logging.INFO, format="    %(message)s")
    payload: dict = {"app": app, "label": APPS[app]}
    try:
        import simstadt
        if app == "pv":
            res = simstadt.photovoltaic_simulation(gml, with_shadows=False, description=APPS[app],
                                                   project_path=project)
        else:
            res = simstadt.heatdemand_simulation(
                gml, with_shadows=False, calculation_mode="HEATING",
                refurbishment_ratio=1.0 if app == "refurb" else 0.0,
                description=APPS[app], project_path=project)
        payload["result_class"] = res.class_name
        payload["output_files"] = [str(p) for p in res.output_files]
        try:
            payload["kpis"] = [k.to_dict() for k in res.kpis]
        except Exception as exc:
            payload["kpi_error"] = f"{type(exc).__name__}: {exc}"
        try:
            df = res.dataframe
            payload["table"] = df.head(3).to_dict("records")
        except Exception as exc:
            payload["table_error"] = f"{type(exc).__name__}: {exc}"
        if hasattr(res, "monthly_df"):
            try:
                monthly = res.monthly_df()
                payload["monthly_mwh"] = {"months": list(monthly.index),
                                          **{col: list(monthly[col]) for col in monthly.columns}}
            except Exception as exc:
                payload["monthly_error"] = f"{type(exc).__name__}: {exc}"
        payload["status"] = "finished"
        code = 0
    except BaseException as exc:  # noqa: BLE001 - report everything to the parent
        payload["status"] = "failed"
        payload["error"] = f"{type(exc).__name__}: {exc}"[-6000:]
        code = 1
    write_json(out, payload)
    return code


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.kill()


def run_app(app: str, gml: Path, project: Path, out: Path, timeout_s: int, env: dict) -> dict:
    out.unlink(missing_ok=True)
    cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", app,
           "--gml", str(gml), "--project", str(project), "--out", str(out)]
    flags = 0x00004000 if os.name == "nt" else 0  # BELOW_NORMAL_PRIORITY_CLASS, inherited by SimStadt
    started = time.monotonic()
    proc = subprocess.Popen(cmd, env=env, creationflags=flags)
    try:
        while True:
            try:
                proc.wait(timeout=30)
                break
            except subprocess.TimeoutExpired:
                elapsed = time.monotonic() - started
                if elapsed > timeout_s:
                    _kill_tree(proc)
                    return {"app": app, "label": APPS[app], "status": "failed", "seconds": round(elapsed),
                            "error": f"Stopped after the {timeout_s // 60} min time limit (--timeout-min)."}
                step(f"  … {APPS[app]} still running ({elapsed:.0f} s)")
    except KeyboardInterrupt:
        _kill_tree(proc)
        raise
    seconds = round(time.monotonic() - started)
    try:
        result = json.loads(out.read_text(encoding="utf-8"))
    except Exception:
        result = {"app": app, "label": APPS[app], "status": "failed",
                  "error": f"SimStadt process ended (exit code {proc.returncode}) without writing a result."}
    result["seconds"] = seconds
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Light SimStadt run on a block model of the building.")
    ap.add_argument("--building", help="Default: the building selected in the dashboard")
    ap.add_argument("--scope", choices=["rooms", "floor", "building"], default="rooms",
                    help="What to simulate: the selected room(s) (default), the whole floor, or the whole building")
    ap.add_argument("--floor", help="Default: the floor selected in the dashboard")
    ap.add_argument("--rooms", help="Comma list of dashboard room names. Default: the room selected in the dashboard")
    ap.add_argument("--apps", default="heat,pv", help="Comma list of: heat, pv, refurb (default heat,pv)")
    ap.add_argument("--year", type=int, default=1960, help="Year of construction assumed for the block (default 1960)")
    ap.add_argument("--function", default="31001_3023",
                    help="ALKIS building function code (default 31001_3023, university building)")
    ap.add_argument("--citygml", type=Path, help="Use this CityGML file instead of the block model")
    ap.add_argument("--box-only", action="store_true", help="Only build the block model; do not run SimStadt")
    ap.add_argument("--timeout-min", type=int, default=15, help="Time limit per application (default 15)")
    ap.add_argument("--max-ram", default="2g", help="Java memory cap for SimStadt (default 2g)")
    ap.add_argument("--worker", choices=list(APPS), help=argparse.SUPPRESS)
    ap.add_argument("--gml", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--project", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--out", type=Path, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.worker:
        return worker(args.worker, args.gml, args.project, args.out)

    apps = [a.strip().lower() for a in args.apps.split(",") if a.strip()]
    unknown = [a for a in apps if a not in APPS]
    if unknown:
        print(f"Unknown application(s): {', '.join(unknown)}. Choose from: {', '.join(APPS)}.")
        return 1

    try:
        selection = json.loads(SELECTION_FILE.read_text(encoding="utf-8"))
    except Exception:
        selection = {}
    building = args.building or selection.get("building")
    if not building:
        print("No building given. Pass --building, or open the SimStadt Sandbox view once.")
        return 1
    floor = args.floor or (selection.get("floor") if not args.building or args.building == selection.get("building") else None)
    if args.rooms:
        room_list = [r.strip() for r in args.rooms.split(",") if r.strip()]
    elif args.scope == "rooms" and (not args.building or args.building == selection.get("building")):
        room_list = [selection["temp_room"]] if selection.get("temp_room") else []
    else:
        room_list = []
    if args.scope == "rooms" and not room_list:
        print("No room given. Pass --rooms 307 (and --floor \"Floor 3\"), or pick a room in the SimStadt Sandbox view once.")
        return 1
    if args.scope in ("rooms", "floor") and not floor:
        print("No floor given. Pass --floor \"Floor 3\".")
        return 1
    scope_label = {"rooms": f"room {', '.join(room_list)} ({floor})" if room_list else "",
                   "floor": f"whole {floor}", "building": "whole building"}[args.scope]
    step(f"Simulating: building {building}, {scope_label}")

    out_dir = light_dir(building)
    results_path = out_dir / "results.json"
    results: dict = {
        "status": "running", "building": str(building), "scope": scope_label,
        "started": datetime.now().isoformat(timespec="seconds"),
        "apps_requested": apps, "apps": {}, "notes": [],
        "assumptions": {"year_of_construction": args.year, "function_code": args.function,
                        "weather": "PVGIS typical year for the HFT Stuttgart location",
                        "geometry": "LoD2 box traced from the IFC rooms, single height, flat roof",
                        "scope": scope_label},
    }

    # 1. geometry -----------------------------------------------------------
    if args.citygml:
        gml = args.citygml.expanduser().resolve()
        if not gml.exists():
            print(f"CityGML file does not exist: {gml}")
            return 1
        results["block"] = {"source": str(gml)}
        results["assumptions"]["geometry"] = f"User CityGML: {gml.name}"
    else:
        step("Building the block model from the IFC rooms (no IFC conversion)…")
        try:
            from temperature_dashboard.ifc_models import viewer_spaces
            if args.scope == "rooms":
                spaces = []
                for room in room_list:
                    spaces += viewer_spaces(str(building), floor, room)
            elif args.scope == "floor":
                spaces = viewer_spaces(str(building), floor)
            else:
                spaces = viewer_spaces(str(building))
            if not spaces:
                raise ValueError("No IFC room geometry for this building. Upload an Architecture IFC in "
                                 "Admin → IFC models, or pass --citygml.")
            if args.scope == "rooms":   # small, exact box: finer grid, thin walls
                fp = trace_footprint(spaces, cell=0.1, gap=0.3, min_courtyard=1.0, wall=0.1)
            else:
                fp = trace_footprint(spaces)
        except Exception as exc:
            results.update(status="failed", error=f"Block model: {exc}",
                           finished=datetime.now().isoformat(timespec="seconds"))
            write_json(results_path, results)
            print(f"Could not build the block model: {exc}")
            return 1
        gml = write_block_citygml(fp, building, out_dir / "block_lod2.gml", args.year, args.function)
        results["block"] = {k: (round(v, 1) if isinstance(v, float) else v)
                            for k, v in fp.items() if k not in ("outer", "holes")}
        results["block"]["outline"] = [[round(x, 2), round(y, 2)] for x, y in fp["outer"]]
        results["block"]["courtyard_outlines"] = [[[round(x, 2), round(y, 2)] for x, y in h] for h in fp["holes"]]
        results["block"]["file"] = str(gml)
        step(f"Block: footprint {fp['footprint_m2']:.0f} m² (rooms cover {fp['room_area_m2']:.0f} m²), "
             f"height {fp['height']:.1f} m, {fp['storeys_above']} storeys above ground, "
             f"{fp['wall_faces']} wall faces, {fp['courtyards']} courtyard(s)")
    write_json(results_path, results)
    if args.box_only:
        results.update(status="block only", finished=datetime.now().isoformat(timespec="seconds"))
        write_json(results_path, results)
        step(f"Block model written: {gml}")
        return 0

    # 2. SimStadt -----------------------------------------------------------
    ok, notes, folder = preflight()
    results["notes"] = notes
    results["simstadt_folder"] = folder
    for note in notes:
        step(f"Note: {note}")
    if not ok:
        results.update(status="failed", error="SimStadt is not installed correctly: " + " ".join(notes),
                       finished=datetime.now().isoformat(timespec="seconds"))
        write_json(results_path, results)
        return 1
    step(f"SimStadt found: {folder}")

    project = out_dir / "simstadt"
    shutil.rmtree(project.with_suffix(".proj"), ignore_errors=True)  # never reuse a stale CityGML copy
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env.setdefault("MAX_RAM", args.max_ram)
    env.setdefault("JAVA_TOOL_OPTIONS", f"-Xmx{args.max_ram}")

    failed = 0
    try:
        for app in apps:
            step(f"Running: {APPS[app]} (usually one to a few minutes; needs internet for PVGIS weather)…")
            results["apps"][app] = {"app": app, "label": APPS[app], "status": "running"}
            write_json(results_path, results)
            res = run_app(app, gml, project, out_dir / f"{app}.json", args.timeout_min * 60, env)
            results["apps"][app] = res
            write_json(results_path, results)
            if res.get("status") == "finished":
                step(f"Finished: {APPS[app]} in {res['seconds']} s")
                for kpi in res.get("kpis", []):
                    unit = f" {kpi['unit']}" if kpi.get("unit") else ""
                    value = kpi.get("value")
                    shown = f"{value:,.{int(kpi.get('precision', 0))}f}" if isinstance(value, (int, float)) else value
                    print(f"           {kpi['name']}: {shown}{unit}")
            else:
                failed += 1
                step(f"FAILED: {APPS[app]}")
                print("           " + str(res.get("error", "unknown error")).replace("\n", "\n           ")[-3000:])
    except KeyboardInterrupt:
        results.update(status="cancelled", finished=datetime.now().isoformat(timespec="seconds"))
        for value in results["apps"].values():
            if value.get("status") == "running":
                value["status"] = "cancelled"
        write_json(results_path, results)
        print("\nCancelled.")
        return 130

    results["status"] = "finished" if failed == 0 else ("failed" if failed == len(apps) else "partly finished")
    results["finished"] = datetime.now().isoformat(timespec="seconds")
    write_json(results_path, results)
    step(f"Done ({results['status']}). Open the SimStadt Sandbox view in the dashboard to see the results.")
    print(f"           Results file: {results_path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
