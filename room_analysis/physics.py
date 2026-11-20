"""Room-level calculations: CO2 decay ventilation, steady-state CO2, UA heat balance.

Pure functions (no Streamlit) so they can be tested on their own.
All of this is a simple steady-state model, good for comparing rooms and scenarios,
not a calibrated simulation.
"""
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

AIR_HEAT_CAPACITY = 0.34          # Wh/(m3 K): rho*cp of air
CO2_PER_PERSON_M3H = 0.018        # m3/h of CO2 per seated, light-activity adult (~18 L/h)
C_OUT_DEFAULT = 420.0             # ppm

# U-values in W/(m2 K). The IFC only has Revit defaults, so these are assumptions.
U_PRESETS = {
    "Unrenovated (1960s-70s)": {"wall": 1.4, "window": 2.8, "door": 3.0, "roof": 0.9, "ground": 1.0},
    "Partly retrofitted (1980s-2000s)": {"wall": 0.8, "window": 1.8, "door": 2.2, "roof": 0.5, "ground": 0.8},
    "Modern (EnEV 2014 / GEG)": {"wall": 0.28, "window": 1.0, "door": 1.5, "roof": 0.2, "ground": 0.35},
}


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    return out.dropna(subset=["timestamp", "value"]).sort_values("timestamp").reset_index(drop=True)


def sample_hours(df: pd.DataFrame) -> float:
    """Typical spacing between readings, in hours (median, robust to gaps)."""
    if len(df) < 2:
        return 0.0
    return float(df["timestamp"].diff().dt.total_seconds().dropna().median() / 3600.0)


def co2_stats(co2: pd.DataFrame, occupied_ppm: float = 800.0, limit_ppm: float = 1000.0) -> dict:
    df = _clean(co2)
    if df.empty:
        return {}
    dt = sample_hours(df)
    return {
        "readings": len(df),
        "hours_covered": round(len(df) * dt, 1),
        "mean_ppm": float(df["value"].mean()),
        "p95_ppm": float(df["value"].quantile(0.95)),
        "peak_ppm": float(df["value"].max()),
        "hours_occupied_proxy": round(float((df["value"] >= occupied_ppm).sum() * dt), 1),
        "hours_above_limit": round(float((df["value"] >= limit_ppm).sum() * dt), 1),
        "hours_above_1400": round(float((df["value"] >= 1400).sum() * dt), 1),
        "share_above_limit": float((df["value"] >= limit_ppm).mean()),
    }


def co2_decay_episodes(co2: pd.DataFrame, c_out: float = C_OUT_DEFAULT, min_minutes: float = 30.0,
                       min_drop_ppm: float = 100.0) -> pd.DataFrame:
    """Air-change rate from CO2 decay after people leave: ln(C - Cout) falls linearly at -n per hour.

    Each falling stretch is fitted separately; the table has one row per usable episode.
    """
    df = _clean(co2)
    cols = ["start", "end", "minutes", "c_start", "c_end", "ach_per_h", "r2"]
    if len(df) < 5:
        return pd.DataFrame(columns=cols)
    df["smooth"] = df["value"].rolling(3, center=True, min_periods=1).median()
    gap_h = max(sample_hours(df) * 3.5, 0.25)
    rows = []
    i, n = 0, len(df)
    while i < n - 2:
        j = i
        while j + 1 < n and (df.at[j + 1, "smooth"] - df.at[j, "smooth"]) <= 10 \
                and (df.at[j + 1, "timestamp"] - df.at[j, "timestamp"]).total_seconds() / 3600 <= gap_h:
            j += 1
        seg = df.iloc[i:j + 1]
        minutes = (seg["timestamp"].iloc[-1] - seg["timestamp"].iloc[0]).total_seconds() / 60
        excess = seg["smooth"] - c_out
        if minutes >= min_minutes and len(seg) >= 4 and excess.iloc[0] >= min_drop_ppm:
            seg = seg[excess > 30]
            excess = seg["smooth"] - c_out
            if len(seg) >= 4:
                t = (seg["timestamp"] - seg["timestamp"].iloc[0]).dt.total_seconds() / 3600
                y = np.log(excess.values)
                slope, intercept = np.polyfit(t.values, y, 1)
                fit = slope * t.values + intercept
                ss_res = float(((y - fit) ** 2).sum())
                ss_tot = float(((y - y.mean()) ** 2).sum()) or 1e-9
                if slope < 0:
                    rows.append({"start": seg["timestamp"].iloc[0], "end": seg["timestamp"].iloc[-1],
                                 "minutes": round(minutes), "c_start": float(seg["smooth"].iloc[0]),
                                 "c_end": float(seg["smooth"].iloc[-1]), "ach_per_h": float(-slope),
                                 "r2": 1 - ss_res / ss_tot})
        i = max(j, i + 1)
    return pd.DataFrame(rows, columns=cols)


def median_ach(episodes: pd.DataFrame, min_r2: float = 0.85) -> tuple[float | None, int]:
    if episodes.empty:
        return None, 0
    good = episodes[(episodes["r2"] >= min_r2) & (episodes["ach_per_h"].between(0.05, 15))]
    if good.empty:
        return None, 0
    return float(good["ach_per_h"].median()), len(good)


def u_values(preset: str, overrides: dict | None = None) -> dict:
    values = dict(U_PRESETS[preset])
    values.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return values


def transmission_breakdown(room: dict, u: dict, b_ground: float = 0.6, b_atrium: float = 0.5) -> dict:
    """UA in W/K per element for one envelope entry (from extract_room_envelope.py)."""
    win = sum(room["window_m2"].values())
    parts = {
        "External walls": room["wall_net_m2"] * u["wall"],
        "Windows": win * u["window"],
        "External doors": room["door_m2"] * u["door"],
        "Roof": room["roof_m2"] * u["roof"],
        "Floor to ground": room["ground_m2"] * u["ground"] * b_ground,
        "Atrium side": (room["atrium_wall_net_m2"] * u["wall"] + room["atrium_window_m2"] * u["window"]) * b_atrium,
    }
    return {k: float(v) for k, v in parts.items()}


def ventilation_conductance(volume_m3: float, ach: float) -> float:
    return AIR_HEAT_CAPACITY * ach * volume_m3


def energy_kwh(h_total_w_per_k: float, indoor: pd.Series, outdoor: pd.Series) -> dict:
    """Heat loss over the window from hourly indoor/outdoor temperatures (only when indoor > outdoor)."""
    both = pd.concat([indoor.rename("ti"), outdoor.rename("to")], axis=1).dropna()
    if both.empty:
        return {"hours": 0, "kwh": 0.0, "mean_dt": float("nan"), "mean_w": float("nan")}
    dt = (both["ti"] - both["to"]).clip(lower=0)
    watts = h_total_w_per_k * dt
    return {"hours": len(both), "kwh": float(watts.sum() / 1000.0), "mean_dt": float(dt.mean()),
            "mean_w": float(watts.mean())}


def steady_state_co2(ach: float, volume_m3: float, persons: float, c_out: float = C_OUT_DEFAULT) -> float:
    if ach <= 0 or volume_m3 <= 0:
        return float("inf")
    return c_out + 1e6 * persons * CO2_PER_PERSON_M3H / (ach * volume_m3)


def required_ach(volume_m3: float, persons: float, limit_ppm: float = 1000.0, c_out: float = C_OUT_DEFAULT) -> float:
    if limit_ppm <= c_out or volume_m3 <= 0:
        return float("nan")
    return 1e6 * persons * CO2_PER_PERSON_M3H / ((limit_ppm - c_out) * volume_m3)


def fetch_outdoor_temperature(start, end, lat: float = 48.7801, lon: float = 9.1700, timeout: float = 15.0):
    """Hourly outdoor temperature (Stuttgart) from Open-Meteo (free, no key). None if offline."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    recent = (pd.Timestamp.now() - end) < pd.Timedelta(days=6)
    base = "https://api.open-meteo.com/v1/forecast" if recent else "https://archive-api.open-meteo.com/v1/archive"
    params = {"latitude": lat, "longitude": lon, "hourly": "temperature_2m", "timezone": "Europe/Berlin"}
    if recent:
        params["past_days"] = max(1, min(92, (pd.Timestamp.now() - start).days + 1))
        params["forecast_days"] = 1
    else:
        params["start_date"] = start.strftime("%Y-%m-%d")
        params["end_date"] = end.strftime("%Y-%m-%d")
    try:
        with urllib.request.urlopen(base + "?" + urllib.parse.urlencode(params), timeout=timeout) as resp:
            data = json.load(resp)
        hourly = data["hourly"]
        series = pd.Series(hourly["temperature_2m"], index=pd.to_datetime(hourly["time"]), dtype=float)
        return series[(series.index >= start.floor("h")) & (series.index <= end.ceil("h"))]
    except Exception:
        return None


def hourly(df: pd.DataFrame) -> pd.Series:
    clean = _clean(df)
    if clean.empty:
        return pd.Series(dtype=float)
    return clean.set_index("timestamp")["value"].resample("1h").mean().dropna()
