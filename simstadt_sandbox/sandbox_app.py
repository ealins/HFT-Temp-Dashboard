from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from temperature_dashboard.db import exists, hierarchy, query


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


# ---------------------------------------------------------------------------
# Read-only SimStadt view. SimStadt itself only ever runs in the terminal:
#   python scripts\run_simstadt_light.py
# This page just reads simstadt_sandbox/outputs/<building>/light/results.json.
# ---------------------------------------------------------------------------
SIMSTADT_RUNS_DIR = Path(__file__).resolve().parent / "outputs"
SELECTION_FILE = SIMSTADT_RUNS_DIR / "current_selection.json"
APP_ORDER = ("heat", "refurb", "pv")


def _room_options(kind: str, building: str, floor: str) -> list[str]:
    if not exists(kind):
        return []
    h = hierarchy(kind)
    mask = h["building"].astype(str) == str(building)
    if floor:
        mask &= h["floor"].astype(str) == str(floor)
    return sorted(h.loc[mask, "room"].astype(str).unique().tolist())


def _remember_selection(building, floor, temp_room, co2_room) -> None:
    """Save the dashboard selection so the terminal runner can use it without arguments."""
    try:
        SIMSTADT_RUNS_DIR.mkdir(parents=True, exist_ok=True)
        SELECTION_FILE.write_text(json.dumps({
            "building": str(building), "floor": str(floor),
            "temp_room": temp_room, "co2_room": co2_room,
        }, indent=2), encoding="utf-8")
    except Exception:
        pass


def _load_results(building: str) -> dict | None:
    path = SIMSTADT_RUNS_DIR / str(building) / "light" / "results.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _kpi_frame(kpis: list[dict]) -> pd.DataFrame:
    rows = []
    for k in kpis or []:
        value = k.get("value")
        if isinstance(value, (int, float)):
            value = f"{value:,.{int(k.get('precision') or 0)}f}"
        rows.append({"Indicator": k.get("name"), "Value": value, "Unit": k.get("unit") or ""})
    return pd.DataFrame(rows)


def _kpi_value(res: dict | None, *needles: str):
    for k in (res or {}).get("kpis", []) or []:
        name = str(k.get("name", "")).lower()
        if all(n in name for n in needles) and isinstance(k.get("value"), (int, float)):
            return k["value"], k.get("unit") or ""
    return None, ""


def _block_figure(block: dict) -> go.Figure | None:
    outline = block.get("outline")
    if not outline:
        return None
    fig = go.Figure()
    ring = outline + [outline[0]]
    fig.add_trace(go.Scatter(x=[p[0] for p in ring], y=[p[1] for p in ring], mode="lines",
                             fill="toself", name="Block outline", line=dict(color="#2563eb", width=2),
                             fillcolor="rgba(37,99,235,0.15)"))
    for hole in block.get("courtyard_outlines") or []:
        ring = hole + [hole[0]]
        fig.add_trace(go.Scatter(x=[p[0] for p in ring], y=[p[1] for p in ring], mode="lines",
                                 fill="toself", name="Courtyard", line=dict(color="#16a34a", width=1.5),
                                 fillcolor="rgba(255,255,255,1)"))
    fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0), showlegend=False,
                      xaxis=dict(visible=False, scaleanchor="y"), yaxis=dict(visible=False))
    return fig


def _monthly_figure(apps: dict) -> go.Figure | None:
    fig = go.Figure()
    for key, color in (("heat", "#dc2626"), ("refurb", "#16a34a")):
        res = apps.get(key) or {}
        monthly = res.get("monthly_mwh")
        if not monthly:
            continue
        cols = [c for c in monthly if c != "months"]
        if not cols:
            continue
        pick = next((c for c in cols if "heat" in c.lower() or "demand" in c.lower()), cols[0])
        fig.add_trace(go.Bar(x=monthly["months"], y=monthly[pick], name=res.get("label", key),
                             marker_color=color))
    if not fig.data:
        return None
    fig.update_layout(barmode="group", height=320, margin=dict(l=0, r=0, t=10, b=0),
                      yaxis_title="MWh per month", legend=dict(orientation="h", y=1.12))
    return fig


def _status_line(results: dict) -> None:
    status = results.get("status", "unknown")
    started = results.get("started", "")
    icon = {"finished": "✅", "running": "⏳", "block only": "🧱", "partly finished": "⚠️",
            "failed": "❌", "cancelled": "⏹️"}.get(status, "•")
    st.markdown(f"**{icon} Last terminal run: {status}** · {results.get('scope', 'whole building')} · started {started}"
                + (f" · finished {results['finished']}" if results.get("finished") else ""))
    if results.get("error"):
        st.error(str(results["error"])[-1500:])


def _results_body(building: str) -> None:
    results = _load_results(building)
    if not results:
        st.warning("No terminal run yet for this building. Run the command below, then this view fills itself.")
        return
    _status_line(results)
    apps = results.get("apps") or {}
    block = results.get("block") or {}

    if block.get("outline"):
        left, right = st.columns([1.2, 1.0])
        with left:
            st.caption("Box simulated by SimStadt (traced from the IFC room geometry, local metres)")
            fig = _block_figure(block)
            if fig:
                st.plotly_chart(fig, use_container_width=True)
        with right:
            st.dataframe(pd.DataFrame([
                ("Footprint", f"{block.get('footprint_m2', 0):,.0f} m²"),
                ("Room area, above ground (IFC)", f"{block.get('room_area_m2', 0):,.0f} m²"),
                ("Height", f"{block.get('height', 0):.1f} m"),
                ("Storeys above / below ground", f"{block.get('storeys_above', '?')} / {block.get('storeys_below', '?')}"),
                ("Courtyards", block.get("courtyards", 0)),
                ("IFC rooms used", block.get("rooms", "")),
            ], columns=["Block", "Value"]), hide_index=True, use_container_width=True)

    # headline numbers
    heat, heat_u = _kpi_value(apps.get("heat"), "heat", "demand")
    refurb, _ = _kpi_value(apps.get("refurb"), "heat", "demand")
    pv, pv_u = _kpi_value(apps.get("pv"), "yield")
    cols = st.columns(3)
    cols[0].metric("Heat demand (as built)", f"{heat:,.0f} {heat_u}" if heat is not None else "–")
    if heat and refurb is not None:
        cols[1].metric("After refurbishment", f"{refurb:,.0f} {heat_u}", f"{(refurb - heat) / heat * 100:+.0f} %", delta_color="inverse")
    else:
        cols[1].metric("After refurbishment", "–")
    cols[2].metric("Rooftop PV", f"{pv:,.0f} {pv_u}" if pv is not None else "–")

    month_fig = _monthly_figure(apps)
    if month_fig:
        st.caption("Monthly heat demand")
        st.plotly_chart(month_fig, use_container_width=True)

    for key in APP_ORDER:
        res = apps.get(key)
        if not res:
            continue
        status = res.get("status")
        with st.expander(f"{res.get('label', key)} · {status}" + (f" · {res.get('seconds')} s" if res.get("seconds") else ""),
                         expanded=(status != "finished")):
            if status == "finished":
                frame = _kpi_frame(res.get("kpis"))
                if not frame.empty:
                    st.dataframe(frame, hide_index=True, use_container_width=True)
                if res.get("output_files"):
                    st.caption("Output files: " + ", ".join(Path(p).name for p in res["output_files"][:8]))
            elif status == "running":
                st.info("Still running in the terminal…")
            else:
                st.error(str(res.get("error", "No details."))[-2500:])

    if results.get("notes"):
        with st.expander("SimStadt installation notes", expanded=results.get("status") == "failed"):
            for note in results["notes"]:
                st.markdown(f"- {note}")
    with st.expander("Assumptions"):
        for k, v in (results.get("assumptions") or {}).items():
            st.markdown(f"- **{k.replace('_', ' ')}**: {v}")
        st.caption("Simplified single-block model: use for relative comparisons (as built vs refurbished), "
                   "not as a calibrated prediction for this building.")


def _live_results(building: str) -> None:
    results = _load_results(building)
    running = bool(results) and results.get("status") == "running"
    fragment = getattr(st, "fragment", None)
    if running and fragment is not None:
        fragment(run_every=5)(_results_body)(building)
    else:
        _results_body(building)
        if running:
            st.caption("Reload the page to refresh the running status.")


def render_simstadt_sandbox(building: str, floor: str,
                            start: pd.Timestamp | None = None, end: pd.Timestamp | None = None):
    st.header(f"SimStadt Sandbox · Building {building}")
    st.info("SimStadt runs in your terminal, never in this page, so the dashboard cannot freeze. "
            "This view only reads the finished results.")

    temp_rooms = _room_options("Temperature", building, floor)
    co2_rooms = _room_options("CO₂", building, floor)
    with st.expander("Observed conditions, latest 24 h (context only)", expanded=False):
        c1, c2 = st.columns(2)
        temp_room = c1.selectbox("Temperature room", temp_rooms or ["–"], key="sim_temp_room")
        co2_room = c2.selectbox("CO₂ room", co2_rooms or ["–"], key="sim_co2_room")
        _remember_selection(building, floor, temp_room, co2_room)
        try:
            latest = query("Temperature", building, temp_room)["timestamp"].max()
            window_end = pd.Timestamp(latest)
            _, _, stats = summarize_sensor_conditions(building, temp_room, co2_room,
                                                      window_end - pd.Timedelta(hours=24), window_end, 800.0)
        except Exception:
            stats = {}
        if stats:
            m = st.columns(3)
            m[0].metric("Mean indoor temp", f"{stats['temperature_mean']:.1f} °C")
            m[1].metric("CO₂ mean / p95", f"{stats['co2_mean']:.0f} / {stats['co2_p95']:.0f} ppm")
            m[2].metric("Occupied share (proxy)", f"{stats['occupied_fraction'] * 100:.0f} %")
        else:
            st.caption("No sensor data for these rooms in the latest 24 h.")

    st.subheader("Results")
    _live_results(building)

    st.subheader("Run it (VS Code terminal, project folder)")
    st.markdown(f"Simulates the room selected above (**{temp_room}**, {floor}). "
                "Quick check first (seconds, no SimStadt), then the real runs.")
    st.code(
        "python scripts\\run_simstadt_light.py --box-only\n"
        "python scripts\\run_simstadt_light.py --apps heat\n"
        "python scripts\\run_simstadt_light.py --apps heat,refurb,pv\n"
        "# other scopes: --scope floor   or   --scope building   or   --rooms 306,307",
        language="powershell",
    )
    st.caption("Heat = monthly balance, light. Refurb = same with full GEG refurbishment (shows savings). "
               "PV = rooftop yield. No shadow simulation, one box, 2 GB Java cap, below-normal priority, 15 min timeout.")
