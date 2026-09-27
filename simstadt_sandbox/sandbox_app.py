from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from temperature_dashboard.db import exists, hierarchy, query
from temperature_dashboard.ifc_models import model_buildings, viewer_spaces


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

    # SimStadt's documented container exposes workflows such as HeatDemand.
    # The project folder is mounted read/write so generated outputs remain
    # accessible to the dashboard.
    cmd = [
        docker,
        "run",
        "--rm",
        "-v",
        f"{citygml_path.parent.resolve()}:/data",
        "simstadt/simstadt",
        "simstadt",
        workflow,
        f"/data/{citygml_path.name}",
        "-p",
        "/data/output",
        "--files",
        "-s",
        f"/data/{output_dir.name}/results.json",
    ]
    return _run_command(cmd)


def run_real_simstadt(citygml_path: Path, workflow: str, output_dir: Path) -> tuple[bool, str]:
    """Execute an actual SimStadt workflow when available.

    Local installations can be supplied via SIMSTADT_COMMAND. Docker users can
    use the official simstadt/simstadt image. No fake result is generated.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    command, source = discover_simstadt()
    if not command:
        return (
            False,
            "No SimStadt executable or Docker installation was found. "
            "Set SIMSTADT_COMMAND / SIMSTADT_EXECUTABLE, or install the SimStadt Docker image.",
        )

    if source == "docker":
        ok, log = _run_simstadt_docker(citygml_path, output_dir, workflow)
    else:
        cmd = [command, workflow, str(citygml_path), "-p", str(output_dir), "--files"]
        ok, log = _run_command(cmd)

    return ok, (f"{source}\n{log}" if log else source)


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
    """Run TUM-GIS IFC→CityGML 3.0 converter; preserve IFC properties/references."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command, source = discover_ifc_converter()
    if not command:
        return False, "No IFC→CityGML converter or Docker runtime was detected."

    if source == "docker":
        # TUM-GIS publishes a ready-to-run multi-arch image.
        cmd = [
            command, "run", "--rm",
            "-v", f"{ifc_path.parent.resolve()}:/app",
            "ghcr.io/tum-gis/ifc-to-citygml3:latest",
            f"/app/{ifc_path.name}",
            "-o", f"/app/{output_path.name}",
        ]
        if georef:
            cmd.append("--georef-oktoberfest")
    else:
        cmd = [command, str(ifc_path), "-o", str(output_path)]
        if georef:
            cmd.append("--georef-oktoberfest")

    return _run_command(cmd, timeout_s=1800)


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
        source = Path(custom_path).expanduser() if custom_path.strip() else None
        if source is None or not source.exists():
            st.error(
                "Provide an existing CityGML file. The current IFC-space extractor is not a "
                "full IFC→CityGML/Energy ADE conversion."
            )
        else:
            run_root = Path(tempfile.gettempdir()) / "hft_simstadt_runs" / building / floor
            run_root.mkdir(parents=True, exist_ok=True)
            output_dir = run_root / "output"
            output_dir.mkdir(parents=True, exist_ok=True)

            with st.spinner(f"Running SimStadt {workflow}…"):
                ok, log = run_real_simstadt(source, workflow, output_dir)

            if ok:
                st.success("SimStadt workflow completed.")
                st.code(log[-12000:] if log else "Completed without textual output.")

                result_files = sorted(output_dir.rglob("*"))
                result_files = [p for p in result_files if p.is_file()]
                if result_files:
                    rows = []
                    for p in result_files:
                        rows.append(
                            {
                                "File": p.name,
                                "Type": p.suffix.lower() or "file",
                                "Size": f"{p.stat().st_size / 1024:.1f} KB",
                            }
                        )
                    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

                    for p in result_files:
                        if p.suffix.lower() == ".csv":
                            try:
                                df = pd.read_csv(p)
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
            else:
                st.error("SimStadt did not complete successfully.")
                st.code(log[-12000:] if log else "No diagnostic log returned.")

    st.divider()
    st.subheader("5 · Research hand-off")

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
