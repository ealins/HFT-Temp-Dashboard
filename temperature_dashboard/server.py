from __future__ import annotations
import requests
import pandas as pd


def fetch_server_data(url: str, token: str = "", timeout: int = 10, fields: dict | None = None) -> pd.DataFrame:
    if not url:
        return pd.DataFrame()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    if isinstance(payload, dict):
        for k in ("data","results","readings","items"):
            if isinstance(payload.get(k), list):
                payload = payload[k]; break
        else:
            payload = [payload]
    df = pd.DataFrame(payload)
    if df.empty: return df
    f = fields or {}
    rename={f.get("timestamp","timestamp"):"timestamp", f.get("value","value"):"value", f.get("sensor","sensor_id"):"sensor", f.get("building","building"):"building", f.get("room","room"):"room"}
    df=df.rename(columns={k:v for k,v in rename.items() if k in df.columns})
    if "timestamp" in df: df["timestamp"]=pd.to_datetime(df["timestamp"],errors="coerce")
    return df
