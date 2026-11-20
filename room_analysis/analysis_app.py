"""Room Analysis view: occupancy and ventilation, room heat balance, room comparison, ventilation vs heat.

Reads sensor data from the dashboard databases and the per-room envelope written by
scripts/extract_room_envelope.py. It never opens the IFC and never runs SimStadt.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from room_analysis import outdoor as outdoor_db
from room_analysis import physics as ph
from temperature_dashboard.db import DATA_DIR, exists, hierarchy, query, room_span

WINDOW_CHOICES = ["Last 24 h", "Last 7 days", "Last 31 days"]


# ---------------------------------------------------------------------------
# data access (all cached, all small)
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=600)
def _load_envelope(building: str) -> dict | None:
    path = DATA_DIR / "ifc_models" / str(building) / "envelope.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


@st.cache_data(show_spinner=False, ttl=600)
def _series(kind: str, building: str, room: str, start, end) -> pd.DataFrame:
    if not exists(kind):
        return pd.DataFrame()
    return query(kind, building, room, start=start, end=end)


@st.cache_data(show_spinner=False, ttl=600)
def _outdoor(start, end, allow_online: bool):
    """Hourly outdoor temperature and its source: local DWD data, else Open-Meteo, else None."""
    local = outdoor_db.load(start, end)
    if local is not None and len(local[0]) >= 3:
        return local
    if allow_online:
        series = ph.fetch_outdoor_temperature(start, end, timeout=8.0)
        if series is not None and len(series):
            return series, "Open-Meteo (Stuttgart)"
    return None, ""


@st.cache_data(show_spinner=False, ttl=600)
def _rooms_with_data(building: str) -> pd.DataFrame:
    rows = []
    for kind, tag in (("Temperature", "T"), ("CO₂", "CO₂")):
        if exists(kind):
            h = hierarchy(kind)
            h = h[h["building"].astype(str) == str(building)]
            for _, r in h.iterrows():
                rows.append({"floor": str(r["floor"]), "room": str(r["room"]), "sensor": tag})
    if not rows:
        return pd.DataFrame(columns=["floor", "room", "sensors"])
    df = pd.DataFrame(rows).drop_duplicates()
    return df.groupby(["floor", "room"])["sensor"].apply(lambda s: " + ".join(sorted(s))).reset_index(name="sensors")


@st.cache_data(show_spinner=False, ttl=600)
def _room_data_span(building: str, room: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """First and last reading of the room over both sensor databases."""
    starts, ends = [], []
    for kind in ("Temperature", "CO₂"):
        span = room_span(kind, building, room) if exists(kind) else {}
        if span:
            starts.append(pd.Timestamp(span["start"]))
            ends.append(pd.Timestamp(span["end"]))
    return (min(starts), max(ends)) if starts else None


def _window(anchor: pd.Timestamp, choice: str, data_start: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Window of 24 h / 7 d / 31 d ending at the anchor (never clamped to the dashboard's own range)."""
    delta = {WINDOW_CHOICES[0]: pd.Timedelta(hours=24), WINDOW_CHOICES[1]: pd.Timedelta(days=7)}.get(choice, pd.Timedelta(days=31))
    return max(anchor - delta, data_start), anchor


def _room_env(env: dict | None, floor: str, room: str) -> dict | None:
    if not env:
        return None
    return env["rooms"].get(f"{floor}|{room}")


TEMP_SOURCES = ["Auto (temperature sensor, else CO₂ sensor thermometer)", "Temperature sensor only", "CO₂ sensor thermometer only"]


def _indoor_temperature(temp_df: pd.DataFrame, co2_df: pd.DataFrame, source: str = TEMP_SOURCES[0]) -> tuple[pd.DataFrame, str]:
    """Indoor temperature. Auto: the room's temperature sensor, else the CO2 sensor's built-in thermometer.
    The two are different devices; the other options force one of them."""
    empty = pd.DataFrame(columns=["timestamp", "value"])
    use_sensor = source in (TEMP_SOURCES[0], TEMP_SOURCES[1])
    use_co2 = source in (TEMP_SOURCES[0], TEMP_SOURCES[2])
    if use_sensor and not temp_df.empty:
        return temp_df[["timestamp", "value"]], "temperature sensor"
    if use_co2 and not co2_df.empty and "secondary_temperature" in co2_df and co2_df["secondary_temperature"].notna().any():
        out = co2_df[["timestamp", "secondary_temperature"]].rename(columns={"secondary_temperature": "value"}).dropna()
        return out, "temperature of the CO₂ sensor"
    return empty, ""


def _fig_base(height=300) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(height=height, margin=dict(l=0, r=0, t=10, b=0), legend=dict(orientation="h", y=1.12))
    return fig


# ---------------------------------------------------------------------------
# tabs
# ---------------------------------------------------------------------------
def _tab_guide():
    """Plain-language method description (room_analysis/GUIDE.md), shown so anyone can see how results are obtained."""
    try:
        st.markdown((Path(__file__).with_name("GUIDE.md")).read_text(encoding="utf-8"))
    except OSError:
        st.info("The method guide (room_analysis/GUIDE.md) was not found.")


def _tab_ventilation(co2: pd.DataFrame, limit: float, c_out: float, episodes: pd.DataFrame, ach: float | None, n_good: int):
    if co2.empty:
        st.info("This room has no CO₂ sensor in the selected window. Pick a CO₂ room, or set the air-change rate "
                "manually under Assumptions.")
        return
    stats = ph.co2_stats(co2, limit_ppm=limit)
    low = float(co2["value"].quantile(0.05))
    if low < c_out - 40:
        st.warning(f"The sensor's lowest readings ({low:.0f} ppm) are below outdoor air ({c_out:.0f} ppm): it probably "
                   "reads low (calibration drift). Hours above the limit are then under-estimated and the decay-based "
                   "air-change rate is uncertain.")
    m = st.columns(5)
    m[0].metric("Mean CO₂", f"{stats['mean_ppm']:.0f} ppm")
    m[1].metric("Peak", f"{stats['peak_ppm']:.0f} ppm")
    m[2].metric(f"Hours above {limit:.0f} ppm", f"{stats['hours_above_limit']:.1f} h",
                f"{stats['share_above_limit'] * 100:.0f} % of time", delta_color="off")
    m[3].metric("Occupied (≥800 ppm proxy)", f"{stats['hours_occupied_proxy']:.1f} h")
    m[4].metric("Air changes (decay)", f"{ach:.2f} /h" if ach else "–", f"{n_good} clean decays" if ach else None, delta_color="off")
    fig = _fig_base()
    fig.add_trace(go.Scatter(x=co2["timestamp"], y=co2["value"], mode="lines", name="CO₂", line=dict(color="#2563eb", width=1.5)))
    fig.add_hline(y=limit, line_dash="dash", line_color="#dc2626", annotation_text=f"{limit:.0f} ppm")
    fig.add_hline(y=c_out, line_dash="dot", line_color="#6b7280", annotation_text="outdoor")
    fig.update_yaxes(title="ppm")
    st.plotly_chart(fig, use_container_width=True)
    with st.expander("How the air-change rate is estimated"):
        st.markdown("After people leave, CO₂ decays towards the outdoor level: ln(C − C_out) falls linearly with slope "
                    "−n (air changes per hour). Each falling stretch of at least 30 min is fitted; the median of the "
                    "fits with R² ≥ 0.85 is used. It assumes a well-mixed room and ignores CO₂ coming from neighbouring rooms.")
        if episodes.empty:
            st.caption("No usable decay episode in this window. Try a longer window.")
        else:
            show = episodes.copy()
            show["ach_per_h"] = show["ach_per_h"].round(2)
            show["r2"] = show["r2"].round(2)
            st.dataframe(show, hide_index=True, use_container_width=True)


def _tab_heat(room_env: dict | None, indoor: pd.DataFrame, indoor_src: str, ach: float, u: dict, b_ground: float,
              b_atrium: float, outdoor: pd.Series | None, outdoor_src: str, outdoor_manual: float, indoor_manual: float,
              t_start, t_end):
    if not room_env:
        st.warning("This room is not in the IFC envelope file. Run `python scripts\\extract_room_envelope.py "
                   "--building <building>` once, or pick a room that has IFC geometry.")
        return
    parts = ph.transmission_breakdown(room_env, u, b_ground, b_atrium)
    h_tr = sum(parts.values())
    h_v = ph.ventilation_conductance(room_env["volume_m3"], ach)
    h_total = h_tr + h_v

    # indoor / outdoor temperature on an hourly grid
    ti = ph.hourly(indoor) if not indoor.empty else pd.Series(dtype=float)
    if ti.empty:
        idx = pd.date_range(pd.Timestamp(t_start).floor("h"), pd.Timestamp(t_end).ceil("h"), freq="1h")
        ti = pd.Series(indoor_manual, index=idx)
        indoor_src = f"manual {indoor_manual:.0f} °C"
    to = outdoor.reindex(ti.index, method="nearest", tolerance=pd.Timedelta("1h")) if outdoor is not None and len(outdoor) else \
        pd.Series(outdoor_manual, index=ti.index)
    energy = ph.energy_kwh(h_total, ti, to)

    m = st.columns(5)
    m[0].metric("Transmission", f"{h_tr:.0f} W/K")
    m[1].metric(f"Ventilation ({ach:.2f} /h)", f"{h_v:.0f} W/K")
    m[2].metric("Mean ΔT in / out", f"{energy['mean_dt']:.1f} K")
    m[3].metric("Mean heat loss", f"{energy['mean_w']:.0f} W", f"{energy['mean_w'] / max(room_env['area_m2'], 1):.0f} W/m²", delta_color="off")
    m[4].metric("Energy in window", f"{energy['kwh']:.1f} kWh", f"{energy['hours']} h", delta_color="off")
    st.caption(f"Indoor temperature from: {indoor_src}. Outdoor: "
               + (f"{outdoor_src}, hourly" if outdoor is not None and len(outdoor)
                  else f"constant {outdoor_manual:.0f} °C (set in Assumptions). No outdoor data for this window: "
                       "run scripts\\ingest_outdoor.py or allow Open-Meteo"))

    left, right = st.columns([1.1, 1.0])
    with left:
        table = pd.DataFrame({"Element": list(parts) + ["Ventilation"], "W/K": list(parts.values()) + [h_v]})
        table["Share"] = table["W/K"] / max(table["W/K"].sum(), 1e-9) * 100
        fig = _fig_base(280)
        t = table.sort_values("W/K")
        fig.add_trace(go.Bar(x=t["W/K"], y=t["Element"], orientation="h", marker_color="#2563eb"))
        fig.update_layout(xaxis_title="Heat loss coefficient (W/K)")
        st.plotly_chart(fig, use_container_width=True)
    with right:
        shown = table.copy()
        shown["W/K"] = shown["W/K"].round(1)
        shown["Share"] = shown["Share"].round(0).astype(int).astype(str) + " %"
        st.dataframe(shown, hide_index=True, use_container_width=True)
        win, wall = room_env["window_m2"], room_env["wall_gross_m2"]
        sides = sorted(set(win) | set(wall))
        st.dataframe(pd.DataFrame({"Facing": sides, "Wall (gross) m²": [round(wall.get(s, 0), 1) for s in sides],
                                   "Windows m²": [round(win.get(s, 0), 1) for s in sides]}),
                     hide_index=True, use_container_width=True)
    st.caption(f"Room {room_env['room']}: {room_env['area_m2']:.0f} m², {room_env['volume_m3']:.0f} m³, "
               f"roof {room_env['roof_m2']:.0f} m², ground {room_env['ground_m2']:.0f} m², "
               f"atrium side {room_env['atrium_wall_net_m2'] + room_env['atrium_window_m2']:.0f} m². "
               "Neighbouring rooms are assumed to be at the same temperature (no heat flow).")

    fig = _fig_base(240)
    watts = (h_total * (ti - to).clip(lower=0)).dropna()
    fig.add_trace(go.Scatter(x=watts.index, y=watts.values, mode="lines", name="Heat loss (W)", line=dict(color="#dc2626")))
    st.plotly_chart(fig, use_container_width=True)


def _tab_compare(building: str, floor: str, rooms: pd.DataFrame, env: dict | None, u: dict, b_ground: float,
                 b_atrium: float, t_start, t_end, temp_source: str = TEMP_SOURCES[0]):
    scope = st.radio("Rooms", [f"This floor ({floor})", "Whole building (slower)"], horizontal=True, key="cmp_scope")
    sub = rooms if scope.startswith("Whole") else rooms[rooms["floor"] == floor]
    sub = sub.head(60)
    if sub.empty:
        st.info("No rooms with sensor data here.")
        return
    rows = []
    bar = st.progress(0.0, text="Reading sensor data…")
    for i, r in enumerate(sub.itertuples()):
        bar.progress((i + 1) / len(sub))
        temp_df = _series("Temperature", building, r.room, t_start, t_end)
        co2_df = _series("CO₂", building, r.room, t_start, t_end)
        indoor, _ = _indoor_temperature(temp_df, co2_df, temp_source)
        row = {"Floor": r.floor, "Room": r.room, "Sensors": r.sensors}
        if not indoor.empty:
            row["Mean °C"] = round(float(indoor["value"].mean()), 1)
            row["% time > 26 °C"] = round(float((indoor["value"] > 26).mean() * 100))
            row["% time < 20 °C"] = round(float((indoor["value"] < 20).mean() * 100))
        if not co2_df.empty:
            s = ph.co2_stats(co2_df)
            row["Peak CO₂"] = round(s["peak_ppm"])
            row["h > 1000 ppm"] = s["hours_above_limit"]
        e = _room_env(env, r.floor, r.room)
        if e:
            win = sum(e["window_m2"].values())
            row["Area m²"] = round(e["area_m2"])
            row["Window m²"] = round(win, 1)
            row["Window/floor %"] = round(win / max(e["area_m2"], 1) * 100)
            row["Main facade"] = max(e["window_m2"], key=e["window_m2"].get) if win > 0 else "–"
            row["Roof"] = "yes" if e["roof_m2"] > 0 else ""
            row["UA W/K"] = round(sum(ph.transmission_breakdown(e, u, b_ground, b_atrium).values()))
        rows.append(row)
    bar.empty()
    table = pd.DataFrame(rows)
    st.dataframe(table, hide_index=True, use_container_width=True)
    c1, c2 = st.columns(2)
    if "h > 1000 ppm" in table and table["h > 1000 ppm"].notna().any():
        t = table.dropna(subset=["h > 1000 ppm"]).sort_values("h > 1000 ppm")
        fig = _fig_base(300)
        fig.add_trace(go.Bar(x=t["h > 1000 ppm"], y=t["Room"].astype(str), orientation="h", marker_color="#dc2626"))
        fig.update_layout(xaxis_title="Hours above 1000 ppm in window")
        with c1:
            st.plotly_chart(fig, use_container_width=True)
    if {"Mean °C", "Window/floor %"} <= set(table.columns) and table[["Mean °C", "Window/floor %"]].dropna().shape[0] >= 2:
        t = table.dropna(subset=["Mean °C", "Window/floor %"])
        fig = _fig_base(300)
        for facade, g in t.groupby("Main facade"):
            fig.add_trace(go.Scatter(x=g["Window/floor %"], y=g["Mean °C"], mode="markers+text", text=g["Room"].astype(str),
                                     textposition="top center", name=f"facing {facade}"))
        fig.update_layout(xaxis_title="Window area / floor area (%)", yaxis_title="Mean indoor °C")
        with c2:
            st.plotly_chart(fig, use_container_width=True)
    st.caption("The window ends at the selected room's last reading, so rooms whose sensors stopped earlier show empty cells. "
               "Temperature comes from the room's temperature sensor, or from the CO₂ sensor's own thermometer when "
               "there is none. Few rooms have enough points to read a trend: treat the scatter as a first look.")


def _tab_scenarios(room_env: dict | None, ach_now: float, c_out: float, limit: float, mean_dt: float,
                   occupied_h_per_day: float, persons: float):
    if not room_env:
        st.warning("This room is not in the IFC envelope file (volume needed).")
        return
    volume = room_env["volume_m3"]
    st.caption(f"Room volume {volume:.0f} m³ · {persons:.0f} people · outdoor CO₂ {c_out:.0f} ppm · mean ΔT {mean_dt:.1f} K")
    left, mid, right = st.columns(3)
    eta = left.slider("Heat recovery (0 = windows / exhaust only)", 0, 90, 0, 5, key="sc_eta") / 100
    days = mid.number_input("Heating days per year", 60, 300, 200, 10, key="sc_days")
    hours = right.number_input("Ventilated hours per day", 1.0, 24.0,
                               float(round(occupied_h_per_day, 1)) if occupied_h_per_day >= 1.0 else 8.0, 0.5,
                               key=f"sc_hours_{room_env['room']}")
    n_req = ph.required_ach(volume, persons, limit, c_out)
    options = {"Now (measured)": ach_now, "0.5 /h": 0.5, "1 /h": 1.0, "2 /h": 2.0, "4 /h": 4.0,
               f"Needed for {limit:.0f} ppm": n_req}
    rows = []
    base_hv = ph.ventilation_conductance(volume, ach_now)
    for label, n in options.items():
        hv = ph.ventilation_conductance(volume, n) * (1 - eta)
        kwh_day = hv * mean_dt * hours / 1000
        rows.append({"Scenario": label, "Air changes /h": round(n, 2),
                     "Steady CO₂ ppm": round(ph.steady_state_co2(n, volume, persons, c_out)),
                     "Ventilation loss W/K": round(hv, 1),
                     "kWh per day": round(kwh_day, 1),
                     "kWh per heating season": round(kwh_day * days),
                     "Extra vs now kWh/season": round((hv - base_hv * (1 - eta)) * mean_dt * hours / 1000 * days)})
    table = pd.DataFrame(rows)
    st.dataframe(table, hide_index=True, use_container_width=True)
    grid = np.linspace(0.1, 6, 60)
    fig = _fig_base(320)
    fig.add_trace(go.Scatter(x=grid, y=[min(ph.steady_state_co2(n, volume, persons, c_out), 5000) for n in grid],
                             mode="lines", name="Steady-state CO₂ (ppm)", line=dict(color="#2563eb")))
    fig.add_hline(y=limit, line_dash="dash", line_color="#dc2626")
    fig.add_trace(go.Scatter(x=grid, y=[ph.ventilation_conductance(volume, n) * (1 - eta) * mean_dt * hours * days / 1000 for n in grid],
                             mode="lines", name="Ventilation heat (kWh / season)", yaxis="y2", line=dict(color="#16a34a")))
    fig.update_layout(xaxis_title="Air changes per hour", yaxis=dict(title="ppm", range=[0, 3500]),
                      yaxis2=dict(title="kWh per season", overlaying="y", side="right"))
    st.plotly_chart(fig, use_container_width=True)
    if n_req == n_req:
        extra = (ph.ventilation_conductance(volume, n_req) - ph.ventilation_conductance(volume, ach_now)) * (1 - eta)
        st.info(f"To keep {persons:.0f} people under {limit:.0f} ppm this room needs about **{n_req:.1f} air changes/h** "
                f"(now {ach_now:.2f}). With {eta * 100:.0f} % heat recovery that is "
                f"{extra * mean_dt * hours * days / 1000:+,.0f} kWh per heating season in ventilation heat.")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def render_room_analysis(building: str, floor: str, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None):
    st.header(f"Room Analysis · Building {building}")
    st.info("Sensor data and IFC geometry only. No SimStadt here: that has its own view. "
            "This is a simple steady-state model, good for comparing rooms and scenarios.")
    if end is None:
        st.warning("No time window available.")
        return
    env = _load_envelope(building)
    rooms = _rooms_with_data(building)
    if rooms.empty:
        st.warning("No rooms with sensor data for this building.")
        return
    if env is None:
        st.warning("No envelope file for this building yet. Run once in the terminal (about a minute):")
        st.code(f"python scripts\\extract_room_envelope.py --building {building}", language="powershell")

    on_floor = rooms[rooms["floor"] == floor]
    pool = on_floor if not on_floor.empty else rooms
    labels = {f"{r.room} · {r.floor} · {r.sensors}": r for r in pool.itertuples()}
    top = st.columns([1.4, 1.0, 1.6, 1.4])
    choice = top[0].selectbox("Room", list(labels), key="ra_room")
    sel = labels[choice]
    win_choice = top[1].selectbox("Window", WINDOW_CHOICES, index=1, key="ra_window")
    data_span = _room_data_span(building, sel.room)
    if data_span is None:
        st.warning("This room has no sensor readings.")
        return
    anchor = min(pd.Timestamp(end), data_span[1])      # the dashboard's end, or the room's last reading if earlier
    t_start, t_end = _window(anchor, win_choice, data_span[0])
    temp_source = top[2].selectbox("Temperature source", TEMP_SOURCES, key="ra_tsrc",
                                   help="Temperature sensors (Excel exports) and the thermometers inside the CO₂ sensors are "
                                        "different devices and may read differently.")
    top[3].caption(f"{t_start:%d %b %Y %H:%M} → {t_end:%d %b %Y %H:%M}  \nRoom data: {data_span[0]:%d %b %Y} → {data_span[1]:%d %b %Y}")

    temp_df = _series("Temperature", building, sel.room, t_start, t_end)
    co2_df = _series("CO₂", building, sel.room, t_start, t_end)
    indoor, indoor_src = _indoor_temperature(temp_df, co2_df, temp_source)
    room_env = _room_env(env, sel.floor, sel.room)

    with st.expander("Assumptions (editable)", expanded=False):
        a1, a2, a3 = st.columns(3)
        preset = a1.selectbox("Construction standard", list(ph.U_PRESETS), index=1, key="ra_preset",
                              help="The IFC carries no usable U-values, so this is an assumption.")
        base_u = ph.U_PRESETS[preset]
        overrides = {k: a2.number_input(f"U {k} (W/m²K)", 0.1, 6.0, float(v), 0.05, key=f"ra_u_{k}_{preset}")
                     for k, v in list(base_u.items())[:3]}
        overrides.update({k: a3.number_input(f"U {k} (W/m²K)", 0.1, 6.0, float(v), 0.05, key=f"ra_u_{k}_{preset}")
                          for k, v in list(base_u.items())[3:]})
        u = ph.u_values(preset, overrides)
        b1, b2, b3, b4 = st.columns(4)
        b_ground = b1.slider("Ground / cellar factor", 0.2, 1.0, 0.6, 0.05, key="ra_bg")
        b_atrium = b2.slider("Atrium side factor", 0.0, 1.0, 0.5, 0.05, key="ra_ba")
        limit = b3.number_input("CO₂ limit (ppm)", 800, 2000, 1000, 50, key="ra_limit")
        c_out = b4.number_input("Outdoor CO₂ (ppm)", 380, 480, 420, 10, key="ra_cout")
        d1, d2, d3, d4 = st.columns(4)
        persons = d1.number_input("People in room (for scenarios)", 1, 200,
                                  int(max(1, round((room_env or {"area_m2": 20})["area_m2"] / 4.0))), 1,
                                  key=f"ra_persons_{sel.floor}_{sel.room}")
        use_weather = d2.checkbox("Allow Open-Meteo if no DWD data", True, key="ra_weather",
                                  help="Outdoor temperature comes from data/outdoor.db (DWD, see scripts/ingest_outdoor.py) when "
                                       "available. Otherwise free hourly Open-Meteo data (needs internet), then the manual value.")
        out_manual = d3.number_input("Outdoor °C (fallback)", -20.0, 35.0, 5.0, 0.5, key="ra_out")
        in_manual = d4.number_input("Indoor °C (if no sensor)", 10.0, 30.0, 20.0, 0.5, key="ra_in")
        ach_source = st.radio("Air-change rate", ["From this room's CO₂ decay", "From another CO₂ room", "Manual"],
                              horizontal=True, key="ra_achsrc")
        ach_manual = st.number_input("Manual air changes per hour", 0.05, 10.0, 0.5, 0.05, key="ra_achman")

    # air-change rate
    co2_for_ach, ach_label = co2_df, "this room"
    if ach_source == "From another CO₂ room":
        co2_rooms = rooms[rooms["sensors"].str.contains("CO₂")]
        other = st.selectbox("CO₂ room to borrow", [f"{r.room} · {r.floor}" for r in co2_rooms.itertuples()], key="ra_other")
        other_room = other.split(" · ")[0]
        co2_for_ach, ach_label = _series("CO₂", building, other_room, t_start, t_end), f"room {other_room}"
    episodes = ph.co2_decay_episodes(co2_for_ach, c_out) if ach_source != "Manual" else pd.DataFrame()
    ach_meas, n_good = ph.median_ach(episodes)
    if ach_source == "Manual" or ach_meas is None:
        ach, ach_note = ach_manual, "manual value" + (" (no usable CO₂ decay found)" if ach_source != "Manual" else "")
    else:
        ach, ach_note = ach_meas, f"CO₂ decay, {ach_label}"
    st.caption(f"Air-change rate used: **{ach:.2f} /h** ({ach_note}) · U-values: wall {u['wall']}, window {u['window']}, roof {u['roof']}")

    outdoor, outdoor_src = _outdoor(t_start, t_end, use_weather)

    tabs = st.tabs(["Occupancy & ventilation", "Heat balance", "Compare rooms", "Ventilation vs heat", "How it works"])
    with tabs[0]:
        _tab_ventilation(co2_for_ach if ach_source == "From another CO₂ room" else co2_df, limit, c_out, episodes, ach_meas, n_good)
    with tabs[1]:
        _tab_heat(room_env, indoor, indoor_src, ach, u, b_ground, b_atrium, outdoor, outdoor_src, out_manual, in_manual, t_start, t_end)
    with tabs[2]:
        _tab_compare(building, sel.floor, rooms, env, u, b_ground, b_atrium, t_start, t_end, temp_source)
    with tabs[3]:
        ti = ph.hourly(indoor) if not indoor.empty else pd.Series(dtype=float)
        if len(ti):
            to = outdoor.reindex(ti.index, method="nearest", tolerance=pd.Timedelta("1h")) if outdoor is not None and len(outdoor) \
                else pd.Series(out_manual, index=ti.index)
            mean_dt = float((ti - to).clip(lower=0).mean())
        else:
            mean_dt = max(in_manual - out_manual, 0.0)
        occupied = ph.co2_stats(co2_for_ach, limit_ppm=limit).get("hours_occupied_proxy", 0.0) if not co2_for_ach.empty else 0.0
        days_in_window = max((t_end - t_start).total_seconds() / 86400, 1e-9)
        _tab_scenarios(room_env, ach, c_out, limit, mean_dt, occupied / days_in_window, persons)
    with tabs[4]:
        _tab_guide()
