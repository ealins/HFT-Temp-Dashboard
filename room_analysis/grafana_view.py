"""Grafana view: embeds the Grafana dashboard inside Streamlit, pre-set to the selected room and time window."""
from __future__ import annotations

import os
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://localhost:3000").rstrip("/")
DASHBOARD_PATH = "/d/hft-rooms/hft-indoor-climate"


def _epoch_ms(ts) -> int:
    """Sensor times are local clock times; the Grafana copy stores them as if UTC, so do the same here."""
    return int(pd.Timestamp(ts).tz_localize("UTC").timestamp() * 1000)


def dashboard_url(building: str, room: str | None, start, end, kiosk: bool = True, floor: str | None = None, flat: bool = False, tsrc: str = "both") -> str:
    params = {"orgId": 1, "from": _epoch_ms(start), "to": _epoch_ms(end), "theme": "light", "var-building": building}
    params["var-tsrc"] = tsrc
    if floor:
        params["var-floor"] = floor
    if room:
        params["var-room"] = f"{building}/{room}" if flat else room
    url = f"{GRAFANA_URL}{DASHBOARD_PATH}?{urllib.parse.urlencode(params)}"
    return url + ("&kiosk&_dash.hideTimePicker=true&_dash.hideVariables=true&_dash.hideLinks=true" if kiosk else "")


@st.cache_data(ttl=30, show_spinner=False)
def grafana_available(timeout: float = 1.0) -> bool:
    """True if Grafana answers. Cached for 30 s so a missing Grafana does not slow every click."""
    try:
        with urllib.request.urlopen(f"{GRAFANA_URL}/api/health", timeout=timeout):
            return True
    except Exception:
        return False


def render_grafana(building: str, floor: str, room: str | None, start, end, tsrc: str = "both") -> None:
    if not grafana_available():
        st.warning(f"Grafana is not reachable at {GRAFANA_URL}. Start it (see README, section Grafana), then reload this view.")
        st.caption("Set the GRAFANA_URL environment variable if Grafana runs on another address.")
        return
    height = st.sidebar.slider("Grafana height (px)", 600, 1600, 1100, 50)
    url = dashboard_url(building, room, start, end, floor=floor, tsrc=tsrc)
    st.caption(f"Building {building}, room {room or '-'}, {pd.Timestamp(start):%d %b %Y} → {pd.Timestamp(end):%d %b %Y}. "
               f"[Open in its own tab]({dashboard_url(building, room, start, end, kiosk=False, floor=floor, tsrc=tsrc)})")
    components.iframe(url, height=height, scrolling=True)
