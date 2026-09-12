from __future__ import annotations

import colorsys

import pandas as pd
import plotly.graph_objects as go

ROLE_COLORS = {
    "Architecture": "#7aa6c2", "HVAC": "#00a6a6", "Structure": "#7f8c8d",
    "Electrical": "#e8a800", "Section": "#8e70b5", "Other": "#6b7b8c",
}


def _value_color(value: float | None, measurement: str) -> str:
    if value is None or pd.isna(value):
        return "#aeb8c2"
    if measurement == "Temperature":
        low, high = 18.0, 28.0
    else:
        low, high = 500.0, 1500.0
    ratio = max(0.0, min(1.0, (float(value) - low) / (high - low)))
    hue = (1.0 - ratio) * 0.62
    r, g, b = colorsys.hsv_to_rgb(hue, 0.72, 0.88)
    return f"rgb({int(r * 255)},{int(g * 255)},{int(b * 255)})"


def build_ifc_figure(spaces: list[dict], latest: dict[tuple[str, str], dict], measurement: str,
                     exploded: bool = False, storey_gap: float = 4.0) -> go.Figure:
    figure = go.Figure()
    storeys = []
    for item in spaces:
        key = (str(item.get("dashboard_floor")), str(item.get("storey_name")))
        if key not in storeys:
            storeys.append(key)
    offsets = {key: index * storey_gap for index, key in enumerate(storeys)} if exploded else {}
    seen_roles: set[str] = set()
    label_x, label_y, label_z, label_text, label_hover = [], [], [], [], []
    labelled_rooms: set[tuple[str, str]] = set()
    for item in spaces:
        geometry = item["geometry"]
        vertices, faces = geometry["vertices"], geometry["faces"]
        x, y = vertices[0::3], vertices[1::3]
        key = (str(item.get("dashboard_floor")), str(item.get("storey_name")))
        z = [value + offsets.get(key, 0.0) for value in vertices[2::3]]
        role = str(item.get("role") or "Other")
        reading = latest.get((str(item.get("dashboard_floor")), str(item.get("dashboard_room"))), {})
        value = reading.get(measurement)
        unit = "°C" if measurement == "Temperature" else "ppm"
        color = _value_color(value, measurement) if role == "Architecture" else ROLE_COLORS.get(role, ROLE_COLORS["Other"])
        hover = (
            f"<b>{item.get('ifc_name') or item.get('long_name') or item['global_id']}</b><br>"
            f"Dashboard room: {item.get('dashboard_room')}<br>Floor: {item.get('dashboard_floor')}<br>"
            f"IFC storey: {item.get('storey_name')}<br>Layer: {role}<br>"
            f"{measurement}: {float(value):.1f} {unit}" if value is not None and not pd.isna(value) else
            f"<b>{item.get('ifc_name') or item.get('long_name') or item['global_id']}</b><br>"
            f"Dashboard room: {item.get('dashboard_room')}<br>Floor: {item.get('dashboard_floor')}<br>"
            f"IFC storey: {item.get('storey_name')}<br>Layer: {role}<br>{measurement}: no reading"
        )
        figure.add_trace(go.Mesh3d(
            x=x, y=y, z=z, i=faces[0::3], j=faces[1::3], k=faces[2::3],
            color=color, opacity=0.82 if role == "Architecture" else 0.45,
            flatshading=True, hovertemplate=hover + "<extra></extra>",
            name=role, legendgroup=role, showlegend=role not in seen_roles,
            lighting={"ambient": 0.65, "diffuse": 0.75, "roughness": 0.85},
        ))
        seen_roles.add(role)
        room_key = (str(item.get("dashboard_floor")), str(item.get("dashboard_room")))
        if role == "Architecture" and room_key not in labelled_rooms:
            label_x.append(sum(x) / len(x))
            label_y.append(sum(y) / len(y))
            label_z.append(sum(z) / len(z))
            room = str(item.get("dashboard_room") or item.get("ifc_name") or "")
            value_text = (f"{float(value):.1f} °C" if measurement == "Temperature" else f"{float(value):.0f} ppm") if value is not None and not pd.isna(value) else ""
            label_text.append(f"<b>{room}</b><br>{value_text}" if value_text else f"<b>{room}</b>")
            label_hover.append(hover)
            labelled_rooms.add(room_key)
    if label_text:
        figure.add_trace(go.Scatter3d(
            x=label_x, y=label_y, z=label_z, mode="text", text=label_text,
            textfont={"color": "#102a43", "size": 12},
            hovertemplate="%{customdata}<extra></extra>", customdata=label_hover,
            name=f"Room {measurement}", showlegend=False,
        ))
    figure.update_layout(
        height=680, margin={"l": 0, "r": 0, "t": 35, "b": 0},
        paper_bgcolor="#f7f9fb", plot_bgcolor="#f7f9fb",
        legend={"title": {"text": "Model layers (click to toggle)"}, "orientation": "h"},
        scene={
            "aspectmode": "data", "dragmode": "orbit",
            "xaxis": {"title": "X", "showbackground": False},
            "yaxis": {"title": "Y", "showbackground": False},
            "zaxis": {"title": "Z", "showbackground": False},
            "camera": {"eye": {"x": 1.5, "y": 1.5, "z": 1.15}},
        },
    )
    return figure
