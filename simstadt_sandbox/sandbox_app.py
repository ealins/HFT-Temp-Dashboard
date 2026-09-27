from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from temperature_dashboard.db import exists, hierarchy, query
from temperature_dashboard.ifc_models import DATA_DIR, list_models, viewer_spaces


# ---------------------------------------------------------------------------
# Sensor-derived boundary conditions
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def model_occupancy(co2_df: pd.DataFrame, threshold_ppm: float = 800.0) -> pd.DataFrame:
    """Create a transparent occupancy proxy from CO2 observations."""
    df = co2_df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp", "value"]).sort_values("timestamp")
    df["occupied"] = (df["value"] >= threshold_ppm).astype(int)
    return df


@st.cache_data(show_spinner=False)
def model_ach(co2_df: pd.DataFrame) -> pd.DataFrame:
    """Approximate room air-change behaviour from CO2 decay.

    This is deliberately labelled as a diagnostic proxy, not a DIN/SimStadt
    ventilation calculation. A single-point decay estimate is not enough to
    identify physical ACH without assumptions about generation and mixing.
    """
    df = co2_df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp", "value"]).sort_values("timestamp")
    df["dt_h"] = df["timestamp"].diff().dt.total_seconds() / 3600.0
    df["dco2"] = df["value"].diff()
    df["decay_fraction_h"] = np.where(
        (df["dt_h"] > 0) & (df["dco2"] < 0),
        (-df["dco2"] / df["dt_h"]) / np.maximum(df["value"].shift(1) - 420.0, 1.0),
        np.nan,
    )
    # Clamp only for visualization robustness.
    df["ach_proxy_h-1"] = df["decay_fraction_h"].clip(lower=0, upper=5)
    return df


@st.cache_data(show_spinner=False)
def summarize_sensor_conditions(
    building: str,
    temp_room: str,
    co2_room: str,
    start: pd.Timestamp | None,
    end: pd.Timestamp | None,
    co2_threshold: float,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    co2 = query("CO₂", building, co2_room, start=start, end=end)
    temp = query("Temperature", building, temp_room, start=start, end=end)
    if co2.empty or temp.empty:
        return co2, temp, {}

    co2 = model_occupancy(co2, co2_threshold)
    co2 = model_ach(co2)

    occupied_fraction = float(co2["occupied"].mean()) if len(co2) else 0.0
    temperature_mean = float(temp["value"].mean())
    temperature_p10 = float(temp["value"].quantile(0.10))
    temperature_p90 = float(temp["value"].quantile(0.90))
    ach_values = co2["ach_proxy_h-1"].dropna()
    ach_median = float(ach_values.median()) if not ach_values.empty else np.nan

    stats = {
        "temperature_mean": temperature_mean,
        "temperature_p10": temperature_p10,
        "temperature_p90": temperature_p90,
        "co2_mean": float(co2["value"].mean()),
        "co2_p95": float(co2["value"].quantile(0.95)),
        "occupied_fraction": occupied_fraction,
        "observed_hours": float(
            (co2["timestamp"].max() - co2["timestamp"].min()).total_seconds() / 3600.0
        )
        if len(co2) > 1
        else 0.0,
        "ach_median_proxy": ach_median,
    }
    return co2, temp, stats


# ---------------------------------------------------------------------------
# SimStadt installation / execution bridge
# ---------------------------------------------------------------------------

def _first_existing(paths: list[str]) -> Path | None:
    for raw in paths:
        if not raw:
            continue
        p = Path(raw).expanduser()
        if p.exists():
            return p
    return None


def discover_simstadt() -> tuple[str | None, str]:
    """Discover a local SimStadt CLI executable or the documented Docker path."""
    env = os.getenv("SIMSTADT_COMMAND", "").strip()
    if env:
        return env, "SIMSTADT_COMMAND"

    candidates = [
        shutil.which("simstadt"),
        shutil.which("simstadtpy"),
        os.getenv("SIMSTADT_EXECUTABLE"),
        os.getenv("SIMSTADT_HOME", ""),
    ]
    exe = _first_existing([x for x in candidates if x])
    if exe:
        return str(exe), "local executable"

    docker = shutil.which("docker")
    if docker:
        return docker, "docker"
    return None, ""


def _run_command(command: list[str], timeout_s: int = 900) -> tuple[bool, str]:
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout_s)
    text = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return proc.returncode == 0, text.strip()


def _run_simstadt_docker(citygml_path: Path, output_dir: Path, workflow: str) -> tuple[bool, str]:
    docker = shutil.which("docker")
    if not docker:
        return False, "Docker is not available on this dashboard host."

    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "simstadt_summary.csv"
    cmd = [
        docker, "run", "--rm",
        "-v", f"{citygml_path.parent.resolve()}:/data",
        "-e", "LOCALE=en_GB",
        "simstadt/simstadt",
        "simstadt", workflow, f"/data/{citygml_path.name}",
        "-p", "/data/output",
        "--files",
        "--csv-export",
        "-s", f"/data/{output_dir.name}/{summary_path.name}",
    ]
    return _run_command(cmd)


def run_real_simstadt(citygml_path: Path, workflow: str, output_dir: Path) -> tuple[bool, str]:
    """Execute a real SimStadt workflow and request CSV exports when available."""
    output_dir.mkdir(parents=True, exist_ok=True)

    command, source = discover_simstadt()
    if not command:
        return (
            False,
            "No SimStadt executable or Docker installation was found. "
            "Set SIMSTADT_COMMAND / SIMSTADT_EXECUTABLE, or install the SimStadt Docker image.",
        )

    summary_path = output_dir / "simstadt_summary.csv"
    if source == "docker":
        ok, log = _run_simstadt_docker(citygml_path, output_dir, workflow)
    else:
        cmd = [
            command, workflow, str(citygml_path),
            "-p", str(output_dir),
            "--files", "--csv-export",
            "-s", str(summary_path),
        ]
        ok, log = _run_command(cmd)

    return ok, (f"{source}\n{log}" if log else source)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _normalise_column(name: object) -> str:
    text = str(name).strip().lower()
    return "".join(ch for ch in text if ch.isalnum())


def _read_simstadt_csv(path: Path) -> pd.DataFrame:
    """Read SimStadt CSVs across German/English separator and decimal conventions."""
    attempts = [
        {"sep": None, "engine": "python"},
        {"sep": ";", "decimal": ","},
        {"sep": ",", "decimal": "."},
    ]
    last_error = None
    for kwargs in attempts:
        try:
            df = pd.read_csv(path, **kwargs)
            if df.shape[1] >= 2:
                df.columns = [str(c).strip() for c in df.columns]
                return df
        except Exception as exc:
            last_error = exc
    raise ValueError(f"Could not parse {path.name}: {last_error}")


def collect_simstadt_csvs(output_dir: Path) -> list[Path]:
    files = []
    roots = [output_dir, Path(str(output_dir) + ".proj"), output_dir.parent]
    seen = set()
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*.csv"):
            if not p.is_file() or p.name.lower() == "sensor_validation.csv":
                continue
            resolved = str(p.resolve())
            if resolved not in seen:
                seen.add(resolved)
                files.append(p)
    return sorted(files, key=lambda p: (p.stat().st_mtime_ns, str(p)))


def parse_simstadt_summary(log: str) -> dict:
    """Extract headline HeatDemand values printed by the current SimStadt CLI."""
    patterns = {
        "specific_heating_kwh_m2a": r"Specific Heating demand\s*:\s*([\d.,]+)\s*kWh",
        "heated_area_m2": r"Heated area\s*:\s*([\d.,]+)\s*m²",
        "yearly_heating_kwh_a": r"Yearly Heating demand\s*:\s*([\d.,]+)\s*kWh",
        "yearly_heating_dhw_kwh_a": r"Total Yearly Heating \+ DHW demand\s*:\s*([\d.,]+)\s*kWh",
        "mean_u_value": r"Mean Uvalue\s*:\s*([\d.,]+)\s*W",
        "year_of_construction": r"Year of construction\s*:\s*([\d.,]+)",
    }
    out = {}
    def parse_number(raw: str) -> float:
        text = str(raw).strip().replace(" ", "")
        if "," in text and "." in text:
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            text = text.replace(",", ".")
        return float(text)

    for key, pattern in patterns.items():
        match = re.search(pattern, log or "", flags=re.IGNORECASE)
        if match:
            try:
                out[key] = parse_number(match.group(1))
            except ValueError:
                pass
    return out


def _direct_series_error_metrics(observed: pd.Series, simulated: pd.Series) -> dict:
    joined = pd.concat([observed.rename("observed"), simulated.rename("simulated")], axis=1).dropna()
    if joined.empty:
        return {}
    error = joined["simulated"] - joined["observed"]
    mae = float(np.mean(np.abs(error)))
    rmse = float(np.sqrt(np.mean(error ** 2)))
    bias = float(np.mean(error))
    denom = float(np.sum((joined["observed"] - joined["observed"].mean()) ** 2))
    r2 = float(1.0 - np.sum(error ** 2) / denom) if denom > 0 else np.nan
    return {
        "n": int(len(joined)),
        "mae": mae,
        "rmse": rmse,
        "bias": bias,
        "r2": r2,
    }


def _find_column(df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    normalized = {_normalise_column(c): c for c in df.columns}
    for candidate in candidates:
        key = _normalise_column(candidate)
        if key in normalized:
            return normalized[key]
    return None


def extract_comparable_simstadt_series(csv_files: list[Path]) -> tuple[pd.DataFrame, list[str]]:
    """Extract only genuine temperature/CO2 measurement series when present."""
    frames = []
    notes = []
    for path in csv_files:
        try:
            df = _read_simstadt_csv(path)
        except Exception:
            continue

        timestamp_col = _find_column(df, ("timestamp", "datetime", "date_time", "date"))
        temp_col = _find_column(df, ("temperature", "ambient temperature", "dry-bulb ambient temperature"))
        co2_col = _find_column(df, ("co2", "co2 ppm", "carbon dioxide ppm"))

        if timestamp_col is None or (temp_col is None and co2_col is None):
            continue

        time = pd.to_datetime(df[timestamp_col], errors="coerce", dayfirst=True)
        item = pd.DataFrame({"timestamp": time}).dropna()
        if temp_col:
            item["sim_temperature"] = pd.to_numeric(df.loc[item.index, temp_col], errors="coerce")
        if co2_col:
            item["sim_co2_ppm"] = pd.to_numeric(df.loc[item.index, co2_col], errors="coerce")
        item = item.dropna(subset=["timestamp"]).drop_duplicates("timestamp")
        if len(item) > 0:
            item["source_file"] = path.name
            frames.append(item)
            notes.append(path.name)

    if not frames:
        return pd.DataFrame(columns=["timestamp", "sim_temperature", "sim_co2_ppm", "source_file"]), notes
    return pd.concat(frames, ignore_index=True).sort_values("timestamp"), notes


def validate_against_sensors(
    sim_series: pd.DataFrame,
    temp_df: pd.DataFrame,
    co2_df: pd.DataFrame,
    threshold: float,
) -> tuple[pd.DataFrame, dict]:
    """Calculate direct metrics only where SimStadt emits the same physical variable."""
    metrics = {}
    comparisons = []

    if not sim_series.empty:
        observed_temp = temp_df.copy()
        observed_temp["timestamp"] = pd.to_datetime(observed_temp["timestamp"], errors="coerce")
        observed_temp["value"] = pd.to_numeric(observed_temp["value"], errors="coerce")
        observed_temp = observed_temp.dropna(subset=["timestamp", "value"]).set_index("timestamp")
        if "sim_temperature" in sim_series.columns:
            s = sim_series.dropna(subset=["sim_temperature"]).set_index("timestamp")["sim_temperature"]
            o = observed_temp["value"].resample("1h").mean()
            s = s.resample("1h").mean()
            m = _direct_series_error_metrics(o, s)
            if m:
                metrics["temperature"] = m
                comparisons.append({"Variable": "Temperature", "Unit": "°C", **m})

        observed_co2 = co2_df.copy()
        observed_co2["timestamp"] = pd.to_datetime(observed_co2["timestamp"], errors="coerce")
        observed_co2["value"] = pd.to_numeric(observed_co2["value"], errors="coerce")
        observed_co2 = observed_co2.dropna(subset=["timestamp", "value"]).set_index("timestamp")
        if "sim_co2_ppm" in sim_series.columns:
            s = sim_series.dropna(subset=["sim_co2_ppm"]).set_index("timestamp")["sim_co2_ppm"]
            o = observed_co2["value"].resample("1h").mean()
            s = s.resample("1h").mean()
            m = _direct_series_error_metrics(o, s)
            if m:
                metrics["co2_ppm"] = m
                comparisons.append({"Variable": "CO₂", "Unit": "ppm", **m})

    comparison_df = pd.DataFrame(comparisons)
    if comparison_df.empty:
        metrics["direct_comparison"] = "unavailable"
        metrics["reason"] = (
            "The SimStadt output contains energy/emission results rather than room Temperature "
            "or CO₂ concentration (ppm). Direct sensor MAE/RMSE is therefore not physically valid."
        )
    else:
        metrics["direct_comparison"] = "available"

    return comparison_df, metrics


def render_validation(
    output_dir: Path,
    temp_df: pd.DataFrame,
    co2_df: pd.DataFrame,
    stats: dict,
    co2_threshold: float,
    log: str,
):
    st.subheader("5 · Automatic validation against HFT sensors")

    summary = parse_simstadt_summary(log)
    if summary:
        st.markdown("**SimStadt headline results**")
        cards = [
            ("Specific heating demand", summary.get("specific_heating_kwh_m2a"), "kWh/(m²·a)"),
            ("Yearly heating demand", summary.get("yearly_heating_kwh_a"), "kWh/a"),
            ("Heating + DHW", summary.get("yearly_heating_dhw_kwh_a"), "kWh/a"),
            ("Heated area", summary.get("heated_area_m2"), "m²"),
        ]
        cols = st.columns(4)
        for col, (label, value, unit) in zip(cols, cards):
            col.metric(label, "n/a" if value is None else f"{value:,.1f} {unit}")

    st.markdown("**Observed boundary conditions used by the Sandbox**")
    if stats:
        a, b, c, d = st.columns(4)
        a.metric("Observed mean temperature", f"{stats['temperature_mean']:.1f} °C")
        b.metric("Observed P95 CO₂", f"{stats['co2_p95']:.0f} ppm")
        c.metric("Occupied proxy", f"{100 * stats['occupied_fraction']:.1f}%")
        d.metric("ACH decay proxy", "n/a" if np.isnan(stats["ach_median_proxy"]) else f"{stats['ach_median_proxy']:.2f} h⁻¹")

    csv_files = collect_simstadt_csvs(output_dir)
    sim_series, sources = extract_comparable_simstadt_series(csv_files)
    comparison_df, metrics = validate_against_sensors(sim_series, temp_df, co2_df, co2_threshold)

    if metrics.get("direct_comparison") == "available":
        st.markdown("**Direct same-variable validation**")
        st.dataframe(comparison_df, width="stretch", hide_index=True)
        for variable, key in (("Temperature", "temperature"), ("CO₂", "co2_ppm")):
            if key not in metrics:
                continue
            m = metrics[key]
            fig = go.Figure()
            observed = temp_df if key == "temperature" else co2_df
            obs = observed.copy()
            obs["timestamp"] = pd.to_datetime(obs["timestamp"], errors="coerce")
            obs = obs.dropna(subset=["timestamp"]).set_index("timestamp")["value"].resample("1h").mean()
            sim = sim_series.dropna(subset=["sim_temperature" if key == "temperature" else "sim_co2_ppm"]).set_index("timestamp")
            sim_col = "sim_temperature" if key == "temperature" else "sim_co2_ppm"
            sim = sim[sim_col].resample("1h").mean()
            fig.add_trace(go.Scatter(x=obs.index, y=obs.values, name="HFT observed", mode="lines"))
            fig.add_trace(go.Scatter(x=sim.index, y=sim.values, name="SimStadt", mode="lines"))
            fig.update_layout(title=f"{variable}: observed vs SimStadt", xaxis_title="Time", yaxis_title=f"{variable} ({'°C' if key == 'temperature' else 'ppm'})")
            st.plotly_chart(fig, width="stretch")
            st.caption(f"MAE {m['mae']:.3f} · RMSE {m['rmse']:.3f} · bias {m['bias']:.3f} · R² {m['r2']:.3f} · n={m['n']:,}")
    else:
        st.info(
            "No direct Temperature/CO₂-concentration series was emitted by this SimStadt workflow. "
            "The dashboard therefore shows the simulated energy/emission outputs and the measured "
            "sensor boundary conditions separately instead of computing an invalid ppm-vs-kWh error."
        )
        boundary = pd.DataFrame([
            {"Observed variable": "Temperature", "Unit": "°C", "Mean": stats.get("temperature_mean"), "P10": stats.get("temperature_p10"), "P90": stats.get("temperature_p90")},
            {"Observed variable": "CO₂", "Unit": "ppm", "Mean": stats.get("co2_mean"), "P95": stats.get("co2_p95"), "Occupied proxy": stats.get("occupied_fraction")},
        ]) if stats else pd.DataFrame()
        if not boundary.empty:
            st.dataframe(boundary, width="stretch", hide_index=True)

    if csv_files:
        st.caption("SimStadt exported CSV files available for inspection:")
        st.dataframe(
            pd.DataFrame([{"File": p.name, "Path": str(p), "Size": f"{p.stat().st_size / 1024:.1f} KB"} for p in csv_files]),
            width="stretch",
            hide_index=True,
        )

    if sim_series.empty:
        st.caption(
            "No same-variable time series was found in the exported SimStadt CSVs. "
            "Heat Demand produces annual/monthly demand outputs; Hourly Heat Demand produces hourly heating demand."
        )

# ---------------------------------------------------------------------------
# IFC/CityGML bridge helpers
# ---------------------------------------------------------------------------

def _write_citygml_from_template(template_text: str, output_path: Path) -> None:
    output_path.write_text(template_text, encoding="utf-8")


def make_minimal_citygml_from_ifc_spaces(building: str, floor: str, spaces: list[dict]) -> str:
    """Create a clearly marked staging CityGML envelope from extracted IFC spaces.

    This is intentionally a lightweight bridge for experimentation. It does
    not claim to preserve full IFC semantics/materials. For production energy
    simulation, use a proper IFC→CityGML/Energy ADE conversion.
    """
    ns = {
        "core": "http://www.opengis.net/citygml/3.0",
        "bldg": "http://www.opengis.net/citygml/building/3.0",
        "gml": "http://www.opengis.net/gml/3.2",
    }
    import xml.etree.ElementTree as ET

    ET.register_namespace("", ns["core"])
    ET.register_namespace("bldg", ns["bldg"])
    ET.register_namespace("gml", ns["gml"])

    root = ET.Element(f"{{{ns['core']}}}CityModel")
    members = ET.SubElement(root, f"{{{ns['core']}}}cityObjectMember")
    building_el = ET.SubElement(members, f"{{{ns['bldg']}}}Building", {f"{{{ns['gml']}}}id": f"hft-{building}"})
    ET.SubElement(building_el, f"{{{ns['gml']}}}name").text = f"HFT Building {building}"

    # Deliberately store the selected IFC space IDs as generic names. Geometry
    # conversion must be handled by the dedicated IFC→CityGML pipeline later.
    for idx, item in enumerate(spaces):
        room_name = item.get("dashboard_room") or item.get("ifc_name") or str(idx + 1)
        ET.SubElement(building_el, f"{{{ns['gml']}}}description").text = (
            f"IFC space {room_name}; floor {floor}"
        )
    return ET.tostring(root, encoding="unicode")


def discover_citygml_inputs(building: str, floor: str) -> list[Path]:
    roots = []
    for env_name in ("SIMSTADT_PROJECT_PATH", "CITYGML_DATA_PATH"):
        value = os.getenv(env_name)
        if value:
            roots.append(Path(value).expanduser())

    candidates = []
    for root in roots:
        if root.exists():
            candidates.extend(root.rglob("*.gml"))
            candidates.extend(root.rglob("*.xml"))

    building_lower = str(building).lower()
    filtered = [
        p for p in candidates
        if building_lower in p.name.lower() or building_lower in str(p.parent).lower()
    ]
    return sorted(filtered or candidates)

def discover_ifc_converter() -> tuple[str | None, str]:
    """Find the TUM IFC→CityGML 3.0 converter via Docker or a local checkout."""
    env = os.getenv("IFC2CITYGML_COMMAND", "").strip()
    if env:
        return env, "IFC2CITYGML_COMMAND"
    for name in ("ifc2citygml",):
        found = shutil.which(name)
        if found:
            return found, "local executable"
    docker = shutil.which("docker")
    if docker:
        return docker, "docker"
    return None, ""


def convert_ifc_to_citygml(ifc_path: Path, output_path: Path, georef: bool = False) -> tuple[bool, str]:
    """Run the TUM-GIS IFC→CityGML 3.0 converter and return its log."""
    ifc_path = ifc_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not ifc_path.exists():
        return False, f"IFC file does not exist: {ifc_path}"

    command, source = discover_ifc_converter()
    if not command:
        return False, "No IFC→CityGML converter or Docker runtime was detected."

    if source == "docker":
        # The generated GML must live in a host-mounted directory. Copy only
        # the selected IFC into the run workspace; the source IFC is untouched.
        workspace = output_path.parent
        docker_ifc = workspace / ifc_path.name
        if docker_ifc.resolve() != ifc_path.resolve():
            shutil.copy2(ifc_path, docker_ifc)

        cmd = [
            command, "run", "--rm",
            "-v", f"{workspace.resolve()}:/app",
            "ghcr.io/tum-gis/ifc-to-citygml3:latest",
            f"/app/{docker_ifc.name}",
            "-o", f"/app/{output_path.name}",
        ]
        if georef:
            cmd.append("--georef-oktoberfest")
    else:
        cmd = [command, str(ifc_path), "-o", str(output_path)]
        if georef:
            cmd.append("--georef-oktoberfest")

    ok, log = _run_command(cmd, timeout_s=1800)
    return ok, log


def get_active_ifc_for_building(building: str) -> tuple[Path | None, str]:
    """Prefer an active Architecture IFC, then any active IFC for the building."""
    models = list_models(building=building, active_only=True)
    if models.empty:
        return None, "No active IFC model is registered for this building."

    preferred = models[models["role"].astype(str).str.lower() == "architecture"]
    row = (preferred.iloc[0] if not preferred.empty else models.iloc[0])
    stored = str(row.get("stored_filename") or "").strip()
    if not stored:
        return None, f"Registered IFC model #{int(row['id'])} has no stored path."

    path = DATA_DIR / stored
    if not path.exists():
        return None, f"Registered IFC model #{int(row['id'])} is missing from disk: {path}"

    return path, f"#{int(row['id'])} · {row['role']} · {row['filename']}"
# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def _room_options(kind: str, building: str, floor: str) -> list[str]:
    if not exists(kind):
        return []
    h = hierarchy(kind)
    mask = h["building"].astype(str) == str(building)
    if floor:
        mask &= h["floor"].astype(str) == str(floor)
    return sorted(h.loc[mask, "room"].astype(str).unique().tolist())


def _sensor_results_table(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    return out[["timestamp", "sensor", "value", "building", "floor", "room"]].tail(5000)


def _save_sensor_profile(co2: pd.DataFrame, temp: pd.DataFrame, stats: dict, path: Path) -> None:
    payload = {
        "description": "Observed HFT room conditions used as SimStadt sandbox diagnostics",
        "stats": stats,
        "temperature_columns": list(temp.columns),
        "co2_columns": list(co2.columns),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def render_simstadt_sandbox(
    building: str,
    floor: str,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
):
    st.header(f"SimStadt Sandbox · Building {building} · {floor}")

    st.info(
        "This sandbox connects the dashboard's IFC and sensor data to SimStadt-oriented "
        "preprocessing and real workflow execution. Sensor-derived occupancy/ACH values "
        "are diagnostics; they are not silently substituted into SimStadt's DIN V 18599 "
        "calculation without a documented conversion step."
    )

    temp_rooms = _room_options("Temperature", building, floor)
    co2_rooms = _room_options("CO₂", building, floor)
    space_items = viewer_spaces(building, floor)
    ifc_room_options = sorted({str(x.get("dashboard_room")) for x in space_items if x.get("dashboard_room")})

    sensor_col, model_col = st.columns([1.0, 1.0])

    with sensor_col:
        st.subheader("1 · Observed boundary conditions")
        temp_room = st.selectbox(
            "Temperature room",
            temp_rooms or [r for r in ifc_room_options] or ["No mapped room"],
            key="sim_temp_room",
        )
        co2_room = st.selectbox(
            "CO₂ room",
            co2_rooms or [r for r in ifc_room_options] or ["No mapped room"],
            key="sim_co2_room",
        )
        threshold = st.slider(
            "CO₂ occupancy proxy threshold",
            min_value=450,
            max_value=2000,
            value=800,
            step=25,
            help="Diagnostic threshold only. It does not alter the standard SimStadt usage model.",
            key="sim_co2_threshold",
        )

        if temp_room != "No mapped room" and co2_room != "No mapped room":
            co2, temp, stats = summarize_sensor_conditions(
                building, temp_room, co2_room, start, end, float(threshold)
            )
        else:
            co2, temp, stats = pd.DataFrame(), pd.DataFrame(), {}

        if stats:
            a, b, c, d = st.columns(4)
            a.metric("Mean room temperature", f"{stats['temperature_mean']:.1f} °C")
            b.metric("Mean CO₂", f"{stats['co2_mean']:.0f} ppm")
            c.metric("Occupied proxy", f"{100 * stats['occupied_fraction']:.1f}%")
            d.metric(
                "ACH decay proxy",
                "n/a" if np.isnan(stats["ach_median_proxy"]) else f"{stats['ach_median_proxy']:.2f} h⁻¹",
            )

            fig = go.Figure()
            if not temp.empty:
                fig.add_trace(go.Scatter(
                    x=temp["timestamp"], y=temp["value"], name="Temperature", mode="lines"
                ))
            if not co2.empty:
                scaled = co2["value"] / max(float(co2["value"].max()), 1.0) * 30
                fig.add_trace(go.Scatter(
                    x=co2["timestamp"], y=scaled, name="CO₂ (scaled)", mode="lines", yaxis="y2"
                ))
            fig.update_layout(
                height=360,
                yaxis={"title": "Temperature (°C)"},
                yaxis2={"title": "CO₂ (scaled)", "overlaying": "y", "side": "right"},
                margin={"l": 40, "r": 40, "t": 25, "b": 35},
            )
            st.plotly_chart(fig, width="stretch")

            with st.expander("Observed records"):
                st.dataframe(
                    pd.concat([_sensor_results_table(temp), _sensor_results_table(co2)], ignore_index=True),
                    width="stretch",
                    hide_index=True,
                )

    with model_col:
        st.subheader("2 · SimStadt workflow input")
        st.write(
            "SimStadt workflows require CityGML. The dashboard already has IFC-space extraction, "
            "so this panel exposes the hand-off rather than pretending IFC is a native SimStadt input."
        )

        candidate_paths = discover_citygml_inputs(building, floor)
        ifc_models = []
        try:
            from temperature_dashboard.ifc_models import list_models
            registered = list_models(building=building, active_only=True)
            if not registered.empty:
                ifc_models = [
                    Path(str(row["filename"])) for _, row in registered.iterrows()
                ]
        except Exception:
            registered = pd.DataFrame()
        options = [str(p) for p in candidate_paths]
        selected = st.selectbox(
            "Existing CityGML input",
            ["None / provide path"] + options,
            key="sim_citygml_choice",
        )

        st.markdown("**IFC → CityGML 3.0 conversion**")
        registered_files = []
        if not registered.empty:
            registered_files = [
                str(Path("data") / str(row["stored_filename"]))
                for _, row in registered.iterrows()
                if row.get("stored_filename")
            ]
        converter, converter_source = discover_ifc_converter()
        if converter:
            st.success(f"IFC→CityGML converter detected: {converter_source}")
        else:
            st.warning("No IFC→CityGML conversion backend detected. Docker is the easiest route.")

        if registered_files:
            selected_ifc = st.selectbox(
                "Registered IFC model",
                ["None"] + registered_files,
                key="sim_ifc_source",
            )
            georef_ifc = st.checkbox(
                "Apply TUM-GIS Munich fallback georeferencing",
                value=False,
                help="Use only when your IFC has no usable georeferencing. The converter's documented fallback targets EPSG:25832 at Theresienwiese, Munich.",
                key="sim_ifc_georef",
            )
            if st.button("Convert selected IFC → CityGML 3.0", key="sim_ifc_convert", type="primary"):
                if selected_ifc == "None":
                    st.warning("Select an IFC model first.")
                else:
                    ifc_source = Path(selected_ifc)
                    if not ifc_source.is_absolute():
                        ifc_source = Path.cwd() / ifc_source
                    output_gml = Path(tempfile.gettempdir()) / "hft_simstadt_runs" / building / floor / f"{ifc_source.stem}.gml"
                    with st.spinner("Converting IFC to CityGML 3.0 with TUM-GIS…"):
                        ok, log = convert_ifc_to_citygml(ifc_source, output_gml, georef_ifc)
                    if ok and output_gml.exists():
                        st.session_state["sim_citygml_generated"] = str(output_gml)
                        st.success(f"CityGML generated: {output_gml}")
                        st.download_button(
                            "Download generated CityGML",
                            output_gml.read_bytes(),
                            file_name=output_gml.name,
                            mime="application/gml+xml",
                            key=f"download_gml_{output_gml.stat().st_mtime_ns}",
                        )
                    else:
                        st.error("IFC→CityGML conversion failed.")
                        st.code(log[-12000:] if log else "No converter log returned.")
        generated_path = st.session_state.get("sim_citygml_generated", "")
        if generated_path and Path(generated_path).exists():
            st.info(f"Using newly generated CityGML: {generated_path}")
            custom_path_default = generated_path
        else:
            custom_path_default = "" if selected == "None / provide path" else selected

        custom_path = st.text_input(
            "CityGML path",
            value=custom_path_default,
            key="sim_citygml_path",
            help="Path visible to the Streamlit/SimStadt execution host.",
        )

        workflow_labels = {
            "HeatDemand": "Heat Demand",
            "HourlyHeatDemand": "Hourly Heat Demand",
            "EnvironmentalAnalysis": "Environmental / CO₂ Analysis",
        }
        workflow = st.selectbox(
            "SimStadt workflow",
            list(workflow_labels),
            format_func=lambda x: workflow_labels[x],
            key="sim_workflow",
        )

        st.caption(
            "The documented SimStadt workflows use a CityGML model plus geometry, physics, "
            "usage and weather processing. Your temperature series is useful for validation; "
            "for a native SimStadt weather input, monthly GHI + ambient temperature are required."
        )

        discovered, discovered_source = discover_simstadt()
        if discovered:
            st.success(f"Execution backend detected: {discovered_source} · {discovered}")
        else:
            st.warning(
                "No SimStadt backend detected. Install SimStadt locally or provide Docker, "
                "then rerun the sandbox."
            )

    st.divider()
    st.subheader("3 · IFC context")

    if space_items:
        left, right = st.columns([1, 1])
        left.metric("Mapped IFC spaces on selected floor", len(space_items))
        right.metric(
            "Spaces with geometry",
            sum(bool(x.get("vertex_count", 0)) for x in space_items),
        )
        with st.expander("IFC space inventory"):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "IFC name": x.get("ifc_name"),
                            "Room": x.get("dashboard_room"),
                            "Storey": x.get("storey_name"),
                            "Vertices": x.get("vertex_count"),
                        }
                        for x in space_items
                    ]
                ),
                width="stretch",
                hide_index=True,
            )
    else:
        st.warning(
            "No extracted IfcSpace geometry is available for this building/floor. "
            "Upload an Architecture IFC in Admin → IFC models."
        )

    st.divider()
    st.subheader("4 · Run real SimStadt workflow")

    run_col, info_col = st.columns([1, 2])
    with run_col:
        run = st.button("▶ Run SimStadt", type="primary", width="stretch")
    with info_col:
        st.caption(
            "No mock results are generated. A run is successful only when a real SimStadt "
            "workflow returns successfully."
        )

    if run:
        run_root = Path(tempfile.gettempdir()) / "hft_simstadt_runs" / str(building) / str(floor)
        run_root.mkdir(parents=True, exist_ok=True)
        output_dir = run_root / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        # Prefer an explicitly supplied CityGML file. Otherwise automatically
        # convert the active IFC model registered for this building.
        source = Path(custom_path).expanduser() if custom_path.strip() else None
        source_label = "Existing CityGML"

        if source is None:
            ifc_source, ifc_label = get_active_ifc_for_building(building)
            if ifc_source is None:
                st.error(ifc_label)
                return

            generated_gml = run_root / f"{ifc_source.stem}.gml"
            needs_conversion = (
                not generated_gml.exists()
                or generated_gml.stat().st_mtime_ns < ifc_source.stat().st_mtime_ns
            )

            if needs_conversion:
                with st.spinner(f"Converting {ifc_label} → CityGML 3.0…"):
                    converted, conversion_log = convert_ifc_to_citygml(
                        ifc_source,
                        generated_gml,
                        georef=False,
                    )
                if not converted or not generated_gml.exists():
                    st.error("Automatic IFC → CityGML 3.0 conversion failed.")
                    st.code(conversion_log[-12000:] if conversion_log else "No converter log returned.")
                    return
            else:
                conversion_log = "Reusing up-to-date generated CityGML."

            source = generated_gml
            source_label = f"Auto-converted from IFC · {ifc_label}"
            st.success(f"CityGML ready: {source.name}")
            with st.expander("IFC → CityGML conversion log"):
                st.code(conversion_log[-12000:] if conversion_log else "Conversion completed.")

        if not source.exists():
            st.error(f"CityGML source does not exist: {source}")
            return

        st.caption(f"SimStadt input: {source_label} · {source}")

        with st.spinner(f"Running SimStadt {workflow}…"):
            ok, log = run_real_simstadt(source, workflow, output_dir)

        if ok:
            st.success("SimStadt workflow completed.")
            st.code(log[-12000:] if log else "Completed without textual output.")

            result_files = sorted(output_dir.rglob("*"))
            result_files = [p for p in result_files if p.is_file()]
            if result_files:
                rows = [
                    {
                        "File": p.name,
                        "Type": p.suffix.lower() or "file",
                        "Size": f"{p.stat().st_size / 1024:.1f} KB",
                    }
                    for p in result_files
                ]
                st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

                for p in result_files:
                    if p.suffix.lower() == ".csv":
                        try:
                            df = _read_simstadt_csv(p)
                            st.markdown(f"**{p.name}**")
                            st.dataframe(df.head(1000), width="stretch", hide_index=True)
                            st.download_button(
                                f"Download {p.name}",
                                p.read_bytes(),
                                file_name=p.name,
                                key=f"download_{p.name}_{p.stat().st_mtime_ns}",
                            )
                        except Exception:
                            pass

            render_validation(
                output_dir,
                temp,
                co2,
                stats,
                float(threshold),
                log,
            )
        else:
            st.error("SimStadt did not complete successfully.")
            st.code(log[-12000:] if log else "No diagnostic log returned.")

    st.divider()
    st.subheader("6 · Research hand-off")

    if stats:
        export_root = Path(tempfile.gettempdir()) / "hft_simstadt_profiles"
        export_root.mkdir(parents=True, exist_ok=True)
        profile = export_root / f"{building}_{floor}_sensor_profile.json".replace(" ", "_")
        _save_sensor_profile(co2, temp, stats, profile)

        st.write(
            "Use the observed profile as a validation/calibration artifact. The defensible "
            "comparison is SimStadt output versus observed building measurements, rather than "
            "calling the sensor-derived proxy itself a SimStadt result."
        )
        st.download_button(
            "Download sensor-derived boundary-condition summary",
            profile.read_bytes(),
            file_name=profile.name,
            mime="application/json",
            width="stretch",
        )


if __name__ == "__main__":
    st.set_page_config(layout="wide")
    render_simstadt_sandbox("1", "Ground floor")
