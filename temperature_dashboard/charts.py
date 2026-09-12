from __future__ import annotations

import math
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go


# HFT Stuttgart Brand Color Palette
HFT_COLORS = {
    "primary": "#003366",        # HFT Dark Blue
    "primary_light": "#0066cc",  # HFT Medium Blue
    "primary_lighter": "#e6f0fa", # HFT Very Light Blue
    "accent_teal": "#009999",    # HFT Teal
    "accent_green": "#00a651",   # HFT Green (Good/Comfort)
    "accent_amber": "#e8a800",   # HFT Amber (Fair/Warning)
    "accent_red": "#cc3333",     # HFT Red (Poor/Alert)
    "accent_dark_red": "#8b0000", # HFT Dark Red (Very Poor)
    "neutral_dark": "#1a2a3a",   # Dark slate for text
    "neutral_grey": "#6b7b8c",   # Medium grey
    "neutral_light": "#d0d8e0",  # Light grey for grids
    "white": "#ffffff",
    "transparent": "rgba(0,0,0,0)",
}

# Consistent color sequences for multi-sensor charts
HFT_SENSOR_COLORS = [
    "#003366",  # HFT Dark Blue
    "#0066cc",  # HFT Medium Blue
    "#009999",  # HFT Teal
    "#00a651",  # HFT Green
    "#e8a800",  # HFT Amber
    "#cc3333",  # HFT Red
    "#6b7b8c",  # Slate grey
    "#8b0000",  # Dark red
]


def rgba(hex_color: str, opacity: float) -> str:
    """Convert #RRGGBB to Plotly-compatible rgba(); Plotly rejects #RRGGBBAA."""
    value = hex_color.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"Expected a six-digit hex color, got {hex_color!r}")
    red, green, blue = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({red},{green},{blue},{opacity})"


def _sampling_gap_threshold(g: pd.DataFrame) -> pd.Timedelta:
    diffs = g["timestamp"].sort_values().diff().dropna()
    diffs = diffs[diffs > pd.Timedelta(0)]
    if diffs.empty:
        return pd.Timedelta(minutes=30)
    median = diffs.median()
    # Break a line after roughly three missed expected samples, with a sensible
    # floor so minor timestamp jitter does not create visual fragmentation.
    return max(median * 3, pd.Timedelta(minutes=30))


def gap_stats(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if df.empty:
        return pd.DataFrame(columns=["sensor", "readings", "first", "last", "median_interval_min", "gaps", "largest_gap_hours", "duplicates"])
    for sensor, g in df.groupby("sensor"):
        g = g.dropna(subset=["timestamp"]).sort_values("timestamp")
        diffs = g["timestamp"].diff().dropna()
        positive = diffs[diffs > pd.Timedelta(0)]
        threshold = _sampling_gap_threshold(g)
        gap_diffs = positive[positive > threshold]
        rows.append(
            {
                "sensor": sensor,
                "readings": len(g),
                "first": g["timestamp"].min(),
                "last": g["timestamp"].max(),
                "median_interval_min": round(positive.median().total_seconds() / 60, 2) if not positive.empty else None,
                "gaps": int(len(gap_diffs)),
                "largest_gap_hours": round(gap_diffs.max().total_seconds() / 3600, 2) if not gap_diffs.empty else 0.0,
                "duplicates": int(g["timestamp"].duplicated().sum()),
            }
        )
    return pd.DataFrame(rows)


def _minmax_decimate_segment(g: pd.DataFrame, max_points: int) -> pd.DataFrame:
    """Reduce drawing load while retaining real observations only (no averaging)."""
    if len(g) <= max_points or max_points < 8:
        return g
    g = g.sort_values("timestamp").reset_index(drop=True)
    # Two extrema per bucket + first/last, giving at most roughly max_points.
    n_buckets = max(1, (max_points - 2) // 2)
    edges = np.linspace(0, len(g), n_buckets + 1, dtype=int)
    keep = {0, len(g) - 1}
    vals = g["value"].to_numpy()
    for i in range(n_buckets):
        a, b = edges[i], edges[i + 1]
        if b <= a:
            continue
        part = vals[a:b]
        valid = np.isfinite(part)
        if not valid.any():
            continue
        valid_idx = np.flatnonzero(valid)
        local = part[valid]
        keep.add(a + int(valid_idx[int(np.argmin(local))]))
        keep.add(a + int(valid_idx[int(np.argmax(local))]))
    return g.iloc[sorted(keep)].copy()


def prepare_plot_data(df: pd.DataFrame, max_points_per_sensor: int = 12000) -> pd.DataFrame:
    """Return shape-preserving real measurements and insert NaN rows across outages.

    No daily/hourly/monthly means are calculated. Long ranges are decimated by
    retaining actual extrema observations from time buckets. Missing periods are
    explicitly represented as NaN so Plotly does not connect across outages.
    """
    if df.empty:
        return df.copy()
    frames = []
    for sensor, source_group in df.groupby("sensor"):
        g = source_group.dropna(subset=["timestamp", "value"]).sort_values("timestamp").copy()
        if g.empty:
            continue
        # If historical and live feeds overlap, keep the newest row for the exact same timestamp.
        g = g.drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)
        threshold = _sampling_gap_threshold(g)
        segment_id = (g["timestamp"].diff() > threshold).cumsum()
        segments = [seg for _, seg in g.groupby(segment_id, sort=False)]
        total = len(g)
        sensor_parts = []
        for idx, seg in enumerate(segments):
            allocation = max(8, int(max_points_per_sensor * len(seg) / max(total, 1)))
            part = _minmax_decimate_segment(seg, allocation)
            sensor_parts.append(part)
            if idx < len(segments) - 1:
                left = seg["timestamp"].iloc[-1]
                right = segments[idx + 1]["timestamp"].iloc[0]
                gap_row = {c: None for c in g.columns}
                gap_row["timestamp"] = left + (right - left) / 2
                gap_row["sensor"] = sensor
                gap_row["value"] = np.nan
                gap_row["source"] = "Missing data"
                sensor_parts.append(pd.DataFrame([gap_row]))
        frames.append(pd.concat(sensor_parts, ignore_index=True))
    return pd.concat(frames, ignore_index=True).sort_values(["sensor", "timestamp"]) if frames else pd.DataFrame(columns=df.columns)


def temperature_time_series(indoor_df: pd.DataFrame) -> go.Figure:
    """Temperature time series with thermal comfort zone shading."""
    fig = go.Figure()
    plot_df = prepare_plot_data(indoor_df)
    
    # Add comfort zone shading (20-24°C) - HFT Green
    fig.add_hrect(y0=20, y1=24, fillcolor=rgba(HFT_COLORS["accent_green"], 0.15), line_width=0,
                  annotation_text="Comfort Zone", annotation_position="top left")
    
    sensors = sorted(plot_df["sensor"].astype(str).unique(), key=lambda x: x.lower())
    for i, sensor in enumerate(sensors):
        g = plot_df[plot_df["sensor"] == sensor]
        color = HFT_SENSOR_COLORS[i % len(HFT_SENSOR_COLORS)]
        fig.add_trace(
            go.Scattergl(
                x=g["timestamp"],
                y=g["value"],
                mode="lines",
                name=str(sensor),
                connectgaps=False,
                hovertemplate=f"{sensor}<br>%{{x|%d %b %Y %H:%M}}<br>%{{y:.2f}} °C<extra></extra>",
                line=dict(width=2, color=color),
            )
        )
    fig.update_layout(
        yaxis_title="Temperature (°C)",
        xaxis_title="Time",
        hovermode="x unified",
        legend_title_text="Sensors",
        height=500,
        margin=dict(l=10, r=10, t=20, b=10),
        plot_bgcolor=HFT_COLORS["transparent"],
        paper_bgcolor=HFT_COLORS["transparent"],
        xaxis=dict(gridcolor=rgba(HFT_COLORS["neutral_light"], 0.25), title_font=dict(color=HFT_COLORS["neutral_dark"])),
        yaxis=dict(gridcolor=rgba(HFT_COLORS["neutral_light"], 0.25), title_font=dict(color=HFT_COLORS["neutral_dark"])),
        font=dict(color=HFT_COLORS["neutral_dark"]),
        legend=dict(font=dict(color=HFT_COLORS["neutral_dark"])),
    )
    fig.update_xaxes(rangeslider_visible=True, rangeslider=dict(bgcolor=HFT_COLORS["primary_lighter"]))
    return fig


def co2_time_series(df: pd.DataFrame, reference: float | None = None) -> go.Figure:
    """CO₂ time series with air quality threshold zones."""
    fig = go.Figure()
    plot_df = prepare_plot_data(df)
    
    # Add CO₂ threshold zones (standard IAQ guidelines) - HFT Colors
    fig.add_hrect(y0=0, y1=800, fillcolor=rgba(HFT_COLORS["accent_green"], 0.15), line_width=0,
                  annotation_text="Good", annotation_position="top left")
    fig.add_hrect(y0=800, y1=1200, fillcolor=rgba(HFT_COLORS["accent_amber"], 0.15), line_width=0,
                  annotation_text="Fair", annotation_position="top left")
    fig.add_hrect(y0=1200, y1=5000, fillcolor=rgba(HFT_COLORS["accent_red"], 0.10), line_width=0,
                  annotation_text="Poor", annotation_position="top left")
    
    sensors = sorted(plot_df["sensor"].astype(str).unique(), key=lambda x: x.lower())
    for i, sensor in enumerate(sensors):
        g = plot_df[plot_df["sensor"] == sensor]
        color = HFT_SENSOR_COLORS[i % len(HFT_SENSOR_COLORS)]
        fig.add_trace(
            go.Scattergl(
                x=g["timestamp"],
                y=g["value"],
                mode="lines",
                name=str(sensor),
                connectgaps=False,
                hovertemplate=f"{sensor}<br>%{{x|%d %b %Y %H:%M}}<br>%{{y:.0f}} ppm<extra></extra>",
                line=dict(width=2, color=color),
            )
        )
    if reference is not None:
        fig.add_hline(y=reference, line_dash="dash", line_color=HFT_COLORS["primary_light"],
                      annotation_text=f"Reference {reference:g} ppm", annotation_position="bottom right")
    fig.update_layout(
        yaxis_title="CO₂ (ppm)",
        xaxis_title="Time",
        hovermode="x unified",
        legend_title_text="Sensors",
        height=500,
        margin=dict(l=10, r=10, t=20, b=10),
        plot_bgcolor=HFT_COLORS["transparent"],
        paper_bgcolor=HFT_COLORS["transparent"],
        xaxis=dict(gridcolor=rgba(HFT_COLORS["neutral_light"], 0.25), title_font=dict(color=HFT_COLORS["neutral_dark"])),
        yaxis=dict(gridcolor=rgba(HFT_COLORS["neutral_light"], 0.25), title_font=dict(color=HFT_COLORS["neutral_dark"])),
        font=dict(color=HFT_COLORS["neutral_dark"]),
        legend=dict(font=dict(color=HFT_COLORS["neutral_dark"])),
    )
    fig.update_xaxes(rangeslider_visible=True, rangeslider=dict(bgcolor=HFT_COLORS["primary_lighter"]))
    return fig


def observed_carpet(df: pd.DataFrame, kind: str, sensor: str) -> go.Figure:
    """Heatmap made from one actual observation nearest each hour midpoint."""
    unit = "°C" if kind == "Temperature" else "ppm"
    x = df[df["sensor"].astype(str) == str(sensor)].dropna(subset=["timestamp", "value"]).copy()
    if x.empty:
        return go.Figure().update_layout(
            title="No observations for selected sensor",
            plot_bgcolor=HFT_COLORS["transparent"],
            paper_bgcolor=HFT_COLORS["transparent"],
            font=dict(color=HFT_COLORS["neutral_dark"]),
        )
    x["date"] = x["timestamp"].dt.date
    x["hour"] = x["timestamp"].dt.hour
    x["minute_distance"] = (x["timestamp"].dt.minute - 30).abs()
    # Actual observation closest to HH:30. No mean/median/interpolation.
    selected = x.sort_values(["date", "hour", "minute_distance"]).drop_duplicates(["date", "hour"], keep="first")
    h = selected.pivot(index="hour", columns="date", values="value").reindex(range(24))
    
    # Choose colorscale based on kind - HFT Colors
    if kind == "Temperature":
        # Temperature: Red gradient (cool red -> warm red -> hot red)
        colorscale = [
            [0, "#ffcccc"],              # Cool - light red/pink
            [0.25, "#ff9999"],           # Mild - light red
            [0.5, HFT_COLORS["accent_red"]],    # Warm - red
            [0.75, "#cc0000"],           # Hot - dark red
            [1, "#8b0000"],              # Very hot - very dark red
        ]
    else:
        # CO2: Green (good) -> Amber (fair) -> Red (poor) -> Dark red (very poor)
        colorscale = [
            [0, HFT_COLORS["accent_green"]],      # Good - green
            [0.33, HFT_COLORS["accent_amber"]],   # Fair - amber
            [0.66, HFT_COLORS["accent_red"]],     # Poor - red
            [1, HFT_COLORS["accent_dark_red"]],   # Very poor - dark red
        ]
    
    fig = px.imshow(
        h,
        aspect="auto",
        origin="lower",
        labels={"x": "Date", "y": "Hour", "color": unit},
        color_continuous_scale=colorscale,
    )
    fig.update_layout(
        height=430, 
        margin=dict(l=10, r=10, t=30, b=10), 
        title=f"Observed hourly snapshot · {sensor}",
        plot_bgcolor=HFT_COLORS["transparent"],
        paper_bgcolor=HFT_COLORS["transparent"],
        xaxis=dict(gridcolor=rgba(HFT_COLORS["neutral_light"], 0.25), title_font=dict(color=HFT_COLORS["neutral_dark"])),
        yaxis=dict(gridcolor=rgba(HFT_COLORS["neutral_light"], 0.25), title_font=dict(color=HFT_COLORS["neutral_dark"])),
        font=dict(color=HFT_COLORS["neutral_dark"]),
        coloraxis_colorbar=dict(
            title_font=dict(color=HFT_COLORS["neutral_dark"]),
            tickfont=dict(color=HFT_COLORS["neutral_dark"]),
        ),
    )
    return fig


def distribution(df: pd.DataFrame, kind: str) -> go.Figure:
    """Distribution histogram with reference zones."""
    unit = "°C" if kind == "Temperature" else "ppm"
    fig = px.histogram(
        df,
        x="value",
        color="sensor",
        color_discrete_sequence=HFT_SENSOR_COLORS,
        marginal="box",
        nbins=60,
        labels={"value": unit, "sensor": "Sensor"},
    )
    
    if kind == "Temperature":
        # Add comfort zone - HFT Green
        fig.add_vrect(x0=20, x1=24, fillcolor=rgba(HFT_COLORS["accent_green"], 0.25), line_width=0,
                      annotation_text="Comfort", annotation_position="top")
    else:
        # Add CO2 zones - HFT Colors
        fig.add_vrect(x0=0, x1=800, fillcolor=rgba(HFT_COLORS["accent_green"], 0.15), line_width=0)
        fig.add_vrect(x0=800, x1=1200, fillcolor=rgba(HFT_COLORS["accent_amber"], 0.15), line_width=0)
        fig.add_vrect(x0=1200, x1=5000, fillcolor=rgba(HFT_COLORS["accent_red"], 0.10), line_width=0)
    
    fig.update_layout(
        height=420, 
        margin=dict(l=10, r=10, t=30, b=10),
        plot_bgcolor=HFT_COLORS["transparent"],
        paper_bgcolor=HFT_COLORS["transparent"],
        xaxis=dict(gridcolor=rgba(HFT_COLORS["neutral_light"], 0.25), title_font=dict(color=HFT_COLORS["neutral_dark"])),
        yaxis=dict(gridcolor=rgba(HFT_COLORS["neutral_light"], 0.25), title_font=dict(color=HFT_COLORS["neutral_dark"])),
        font=dict(color=HFT_COLORS["neutral_dark"]),
        legend=dict(font=dict(color=HFT_COLORS["neutral_dark"])),
    )
    return fig


def coverage_timeline(df: pd.DataFrame) -> go.Figure:
    """Show individual observations over time, useful for seeing outages directly."""
    if df.empty:
        return go.Figure().update_layout(
            plot_bgcolor=HFT_COLORS["transparent"],
            paper_bgcolor=HFT_COLORS["transparent"],
            font=dict(color=HFT_COLORS["neutral_dark"]),
        )
    x = df.dropna(subset=["timestamp"]).copy()
    # Keep real timestamps only; sample for rendering if extremely dense.
    frames = []
    for sensor, g in x.groupby("sensor"):
        g = g.sort_values("timestamp")
        if len(g) > 8000:
            idx = np.linspace(0, len(g) - 1, 8000, dtype=int)
            g = g.iloc[np.unique(idx)]
        frames.append(g)
    x = pd.concat(frames, ignore_index=True) if frames else x.iloc[0:0]
    fig = px.scatter(x, x="timestamp", y="sensor", labels={"timestamp": "Time", "sensor": "Sensor"},
                     color="sensor", color_discrete_sequence=HFT_SENSOR_COLORS)
    fig.update_traces(marker={"size": 4, "opacity": 0.65})
    fig.update_layout(
        height=max(280, 55 * max(1, x["sensor"].nunique())), 
        margin=dict(l=10, r=10, t=20, b=10),
        plot_bgcolor=HFT_COLORS["transparent"],
        paper_bgcolor=HFT_COLORS["transparent"],
        xaxis=dict(gridcolor=rgba(HFT_COLORS["neutral_light"], 0.25), title_font=dict(color=HFT_COLORS["neutral_dark"])),
        yaxis=dict(gridcolor=rgba(HFT_COLORS["neutral_light"], 0.25), title_font=dict(color=HFT_COLORS["neutral_dark"])),
        font=dict(color=HFT_COLORS["neutral_dark"]),
        legend=dict(font=dict(color=HFT_COLORS["neutral_dark"])),
    )
    return fig
