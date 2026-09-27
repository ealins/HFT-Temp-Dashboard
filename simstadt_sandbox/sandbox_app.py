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


def _simstadt_max_ram() -> str:
    """Choose a conservative JVM heap cap so SimStadt cannot starve Streamlit."""
    configured = os.getenv("SIMSTADT_MAX_RAM", "").strip()
    if configured:
        return configured

    try:
        if os.name == "nt":
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                total_gb = status.ullTotalPhys / (1024 ** 3)
                if total_gb <= 8:
                    return "2g"
                if total_gb <= 16:
                    return "3g"
                return "4g"
    except Exception:
        pass

    return "4g"


def _simstadt_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["MAX_RAM"] = _simstadt_max_ram()
    env.setdefault("LOCALE", "en_GB")
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def discover_simstadt() -> tuple[str | None, str]:
    """Prefer the local simstadt CLI, then Docker."""
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

    # Common Windows installations/downloads. SimStadt.bat supports CLI mode
    # when called with a path/workflow argument.
    common_roots = [
        Path.home() / "Desktop",
        Path.home() / "Downloads",
        Path(os.getenv("SIMSTADT_HOME", "")) if os.getenv("SIMSTADT_HOME") else None,
    ]
    for root in common_roots:
        if root and root.exists():
            matches = sorted(root.glob("SimStadt*/SimStadt.bat"))
            if matches:
                return str(matches[0]), "SimStadt.bat"

    try:
        import importlib.util
        if importlib.util.find_spec("simstadt") is not None:
            return sys.executable, "python module"
    except Exception:
        pass

    docker = shutil.which("docker")
    if docker:
        return docker, "docker"
    return None, ""


def _local_simstadt_command(
    command: str,
    source: str,
    workflow: str,
    citygml_path: Path,
    output_dir: Path,
    summary_path: Path,
) -> list[str]:
    if source == "python module":
        return [
            command, "-m", "simstadt", workflow, str(citygml_path),
            "-p", str(output_dir), "--files", "--csv-export",
            "-s", str(summary_path),
        ]
    if source == "SimStadt.bat":
        return [
            command, str(citygml_path),
            "-p", str(output_dir), "--files", "--csv-export",
            "-s", str(summary_path),
        ]
    return [
        command, workflow, str(citygml_path),
        "-p", str(output_dir), "--files", "--csv-export",
        "-s", str(summary_path),
    ]


def _run_command(
    command: list[str],
    timeout_s: int = 900,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=env or _simstadt_environment(),
    )
    text = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return proc.returncode == 0, text.strip()


def _background_job_file(output_dir: Path) -> Path:
    return output_dir.parent / "simstadt_job.json"


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def start_simstadt_background(citygml_path: Path, workflow: str, output_dir: Path) -> tuple[bool, str]:
    """Launch the real SimStadt process without blocking Streamlit."""
    output_dir.mkdir(parents=True, exist_ok=True)
    job_file = _background_job_file(output_dir)

    if job_file.exists():
        try:
            old = json.loads(job_file.read_text(encoding="utf-8"))
            if old.get("state") == "running" and _pid_alive(old.get("pid")):
                return False, "A SimStadt simulation is already running."
        except Exception:
            pass

    command, source = discover_simstadt()
    if not command:
        return False, (
            "No SimStadt runtime was found. Install SimStadt locally with "
            "scripts/setup_simstadt.ps1, set SIMSTADT_COMMAND/SIMSTADT_HOME, "
            "or install Docker Desktop as the bundled runtime fallback."
        )

    summary_path = output_dir / "simstadt_summary.csv"
    if source == "docker":
        cmd = [
            command, "run", "--rm",
            "-v", f"{citygml_path.parent.resolve()}:/data",
            "-e", "LOCALE=en_GB",
            "simstadt/simstadt:cli",
            "simstadt", workflow, f"/data/{citygml_path.name}",
            "-p", "/data/output",
            "--files", "--csv-export",
            "-s", f"/data/output/{summary_path.name}",
        ]
    else:
        cmd = _local_simstadt_command(
            command, source, workflow, citygml_path, output_dir, summary_path
        )

    log_path = output_dir.parent / "simstadt_worker.log"
    for child in output_dir.iterdir():
        if child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child, ignore_errors=True)

    log = log_path.open("w", encoding="utf-8")
    popen_kwargs = {
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "start_new_session": True,
        "close_fds": True,
        "text": True,
        "env": _simstadt_environment(),
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    proc = subprocess.Popen(cmd, **popen_kwargs)
    log.close()

    job = {
        "state": "running",
        "pid": proc.pid,
        "workflow": workflow,
        "source": source,
        "started_at": pd.Timestamp.utcnow().isoformat(),
        "input": str(citygml_path),
        "output": str(output_dir),
        "log": str(log_path),
        "returncode": None,
    }
    job_file.write_text(json.dumps(job, indent=2), encoding="utf-8")
    return True, (
        f"Started SimStadt {workflow} in the background "
        f"(JVM memory cap: {_simstadt_max_ram()})."
    )


def get_simstadt_background_status(output_dir: Path) -> dict:
    job_file = _background_job_file(output_dir)
    if not job_file.exists():
        return {"state": "idle"}

    try:
        job = json.loads(job_file.read_text(encoding="utf-8"))
    except Exception:
        return {"state": "starting"}

    if job.get("state") == "running":
        pid = job.get("pid")
        if _pid_alive(pid):
            return job

        output_dir = Path(job.get("output", ""))
        successful_outputs = (
            (output_dir / "simstadt_summary.csv").exists()
            or any(output_dir.rglob("*.csv"))
        )
        job["state"] = "finished" if successful_outputs else "failed"
        job["returncode"] = 0 if successful_outputs else -1
        job_file.write_text(json.dumps(job, indent=2), encoding="utf-8")

    try:
        job["log_tail"] = Path(job.get("log", "")).read_text(
            encoding="utf-8", errors="replace"
        )[-12000:]
    except Exception:
        job["log_tail"] = ""

    return job


def stop_simstadt_background(output_dir: Path) -> tuple[bool, str]:
    job_file = _background_job_file(output_dir)
    status = get_simstadt_background_status(output_dir)
    pid = status.get("pid")
    if status.get("state") != "running" or not pid:
        return False, "No running SimStadt simulation was found."

    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
            )
        else:
            os.kill(int(pid), 15)
        status["state"] = "cancelled"
        status["returncode"] = -15
        job_file.write_text(json.dumps(status, indent=2), encoding="utf-8")
        return True, "SimStadt cancellation requested."
    except Exception as exc:
        return False, f"Could not stop SimStadt: {exc}"



def _render_simstadt_completed_results(
    output_dir: Path,
    temp_df: pd.DataFrame,
    co2_df: pd.DataFrame,
    stats: dict,
    threshold: float,
):
    status = get_simstadt_background_status(output_dir)
    state = status.get("state", "idle")

    if state == "running":
        st.info("SimStadt is running in the background. Streamlit remains responsive.")
        with st.expander("Live SimStadt output", expanded=False):
            st.code(status.get("log_tail", "") or "Waiting for SimStadt output…")
        return

    if state == "finished":
        log = status.get("log_tail", "")
        st.success("SimStadt workflow completed.")
        with st.expander("SimStadt output", expanded=False):
            st.code(log or "No textual output returned.")

        files = [p for p in output_dir.rglob("*") if p.is_file()]
        if files:
            st.dataframe(
                pd.DataFrame([
                    {
                        "File": p.name,
                        "Type": p.suffix.lower() or "file",
                        "Size": f"{p.stat().st_size / 1024:.1f} KB",
                    }
                    for p in files
                ]),
                width="stretch",
                hide_index=True,
            )
            for p in files:
                if p.suffix.lower() == ".csv":
                    try:
                        df = _read_simstadt_csv(p)
                    except Exception:
                        continue
                    with st.expander(f"Result · {p.name}", expanded=False):
                        st.dataframe(df.head(1000), width="stretch", hide_index=True)
                        st.download_button(
                            f"Download {p.name}",
                            p.read_bytes(),
                            file_name=p.name,
                            key=f"sim_result_{p.name}_{p.stat().st_mtime_ns}",
                        )

        render_validation(
            output_dir,
            temp_df,
            co2_df,
            stats,
            threshold,
            log,
        )
        return

    if state in {"failed", "cancelled"}:
        st.error(f"SimStadt job state: {state}.")
        with st.expander("SimStadt diagnostics", expanded=True):
            st.code(status.get("log_tail", "") or "No worker output returned.")
        return

    if state == "idle":
        st.caption("No SimStadt simulation is currently running.")




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

IFC_CITYGML_TOOLS = Path(__file__).resolve().parents[1] / ".tools" / "ifc-to-citygml3"
IFC_CITYGML_SCRIPT = IFC_CITYGML_TOOLS / "ifc2citygml.py"
IFC_CITYGML_VENV_PYTHON = IFC_CITYGML_TOOLS / ".venv" / "Scripts" / "python.exe"


def _converter_python() -> str:
    """Use the dedicated converter environment when the bundled setup exists."""
    candidates = [
        IFC_CITYGML_VENV_PYTHON,
        IFC_CITYGML_TOOLS / ".venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which("python") or os.getenv("PYTHON", "python")


def _conversion_manifest_path(output_path: Path) -> Path:
    return output_path.with_suffix(output_path.suffix + ".json")


def _conversion_cache_is_valid(
    ifc_path: Path,
    output_path: Path,
    georef: bool,
    converter_id: str,
) -> bool:
    if not output_path.exists() or output_path.stat().st_size == 0:
        return False
    manifest_path = _conversion_manifest_path(output_path)
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        stat = ifc_path.stat()
        return (
            manifest.get("source") == str(ifc_path.resolve())
            and manifest.get("source_mtime_ns") == stat.st_mtime_ns
            and manifest.get("source_size") == stat.st_size
            and bool(manifest.get("georef")) == bool(georef)
            and manifest.get("converter") == converter_id
        )
    except Exception:
        return False


def _write_conversion_manifest(
    ifc_path: Path,
    output_path: Path,
    georef: bool,
    converter_id: str,
) -> None:
    stat = ifc_path.stat()
    _conversion_manifest_path(output_path).write_text(
        json.dumps(
            {
                "source": str(ifc_path.resolve()),
                "source_mtime_ns": stat.st_mtime_ns,
                "source_size": stat.st_size,
                "georef": bool(georef),
                "converter": converter_id,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def discover_ifc_converter() -> tuple[str | None, str]:
    """Prefer direct local TUM-GIS conversion; use Docker only as fallback."""
    env = os.getenv("IFC2CITYGML_COMMAND", "").strip()
    if env:
        return env, "IFC2CITYGML_COMMAND"

    executable = shutil.which("ifc2citygml")
    if executable:
        return executable, "local executable"

    script_env = os.getenv("IFC2CITYGML_SCRIPT", "").strip()
    if script_env and Path(script_env).expanduser().exists():
        return str(Path(script_env).expanduser()), "IFC2CITYGML_SCRIPT"

    repo_root = Path(__file__).resolve().parents[1]
    script_candidates = [
        IFC_CITYGML_SCRIPT,
        repo_root / "ifc2citygml.py",
        repo_root / "ifc-to-citygml3" / "ifc2citygml.py",
        repo_root.parent / "ifc-to-citygml3" / "ifc2citygml.py",
        Path.cwd() / "ifc2citygml.py",
    ]
    for script in script_candidates:
        if script.exists():
            source = (
                "managed local converter"
                if script.resolve() == IFC_CITYGML_SCRIPT.resolve()
                else "local converter script"
            )
            return str(script), source

    docker = shutil.which("docker")
    if docker:
        return docker, "docker fallback"
    return None, ""


def convert_ifc_to_citygml(
    ifc_path: Path,
    output_path: Path,
    georef: bool = False,
) -> tuple[bool, str]:
    """Convert IFC to CityGML, preferring direct local execution and cached output."""
    ifc_path = ifc_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not ifc_path.exists():
        return False, f"IFC file does not exist: {ifc_path}"

    command, source = discover_ifc_converter()
    if not command:
        return (
            False,
            "No IFC-to-CityGML converter is installed. Run scripts/setup_ifc2citygml.ps1 "
            "for the recommended local setup, or install Docker as a fallback.",
        )

    command_path = Path(command).expanduser()
    command_version = (
        f":{command_path.stat().st_mtime_ns}"
        if source in {"managed local converter", "local converter script", "IFC2CITYGML_SCRIPT"}
        and command_path.exists()
        else ""
    )
    converter_id = f"{source}:{command}{command_version}"
    if _conversion_cache_is_valid(ifc_path, output_path, georef, converter_id):
        return True, "Cached CityGML is up to date; IFC geometry conversion was skipped."

    output_path.unlink(missing_ok=True)

    if source == "docker fallback":
        workspace = output_path.parent
        docker_ifc = workspace / ifc_path.name
        if docker_ifc.resolve() != ifc_path.resolve():
            shutil.copy2(ifc_path, docker_ifc)

        cmd = [
            command,
            "run",
            "--rm",
            "-v",
            f"{workspace.resolve()}:/app",
            "ghcr.io/tum-gis/ifc-to-citygml3:latest",
            f"/app/{docker_ifc.name}",
            "-o",
            f"/app/{output_path.name}",
        ]
    elif source in {
        "IFC2CITYGML_SCRIPT",
        "local converter script",
        "managed local converter",
    }:
        cmd = [
            _converter_python(),
            command,
            str(ifc_path),
            "-o",
            str(output_path),
        ]
    else:
        cmd = [command, str(ifc_path), "-o", str(output_path)]

    if georef:
        cmd.append("--georef-oktoberfest")

    ok, log = _run_command(cmd, timeout_s=1800)
    if ok and output_path.exists() and output_path.stat().st_size > 0:
        _write_conversion_manifest(ifc_path, output_path, georef, converter_id)
        return True, log or f"Converted with {source}."

    return False, log or "IFC-to-CityGML conversion did not produce an output file."


def _registered_ifc_path(row: pd.Series) -> Path | None:
    """Resolve the current registry path, including legacy records created before stored_filename was exposed."""
    stored = str(row.get("stored_filename") or "").strip()
    if stored:
        candidate = DATA_DIR / stored
        if candidate.exists():
            return candidate

    model_id = row.get("id")
    if model_id is not None:
        legacy = DATA_DIR / "ifc_models" / str(int(model_id)) / "model.ifc"
        if legacy.exists():
            return legacy

    return None


def get_active_ifc_for_building(building: str) -> tuple[Path | None, str]:
    """Prefer an active Architecture IFC, then any active IFC for the building."""
    models = list_models(building=building, active_only=True)
    if models.empty:
        return None, "No active IFC model is registered for this building."

    preferred = models[models["role"].astype(str).str.lower() == "architecture"]
    row = (preferred.iloc[0] if not preferred.empty else models.iloc[0])
    path = _registered_ifc_path(row)
    if path is None:
        return None, (
            f"Registered IFC model #{int(row['id'])} ({row['filename']}) "
            "cannot be found in the dashboard data directory. Re-upload the IFC in Admin → IFC models."
        )

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
        registered_paths: dict[str, Path] = {}
        if not registered.empty:
            for _, row in registered.iterrows():
                resolved = _registered_ifc_path(row)
                if resolved is not None:
                    label = f"#{int(row['id'])} · {row['role']} · {row['filename']}"
                    registered_files.append(label)
                    registered_paths[label] = resolved
        converter, converter_source = discover_ifc_converter()
        if converter:
            if converter_source in {"managed local converter", "local executable", "IFC2CITYGML_SCRIPT", "local converter script"}:
                st.success(f"Direct local IFC-to-CityGML converter detected: {converter_source}")
            else:
                st.info("Docker IFC-to-CityGML converter detected as fallback; local conversion is not installed.")
        else:
            st.warning(
                "No IFC-to-CityGML converter is available. Recommended: run "
                "scripts/setup_ifc2citygml.ps1 once to install the direct local converter. "
                "Docker remains a fallback."
            )

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
                    ifc_source = registered_paths.get(selected_ifc)
                    if ifc_source is None:
                        st.error("The selected IFC could not be resolved on disk. Re-upload it in Admin → IFC models.")
                        st.stop()
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

    run_root = Path(tempfile.gettempdir()) / "hft_simstadt_runs" / str(building) / str(floor)
    output_dir = run_root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    job_status = get_simstadt_background_status(output_dir)

    run_col, stop_col, info_col = st.columns([1.25, 0.8, 2])
    with run_col:
        run = st.button(
            "▶ Start SimStadt in background",
            type="primary",
            width="stretch",
            disabled=job_status.get("state") == "running",
        )
    with stop_col:
        stop = st.button(
            "■ Stop",
            width="stretch",
            disabled=job_status.get("state") != "running",
        )
    with info_col:
        st.caption(
            "Simulation runs outside the Streamlit request. The page remains responsive "
            "and polls the worker for completion."
        )

    if run:
        source = Path(custom_path).expanduser() if custom_path.strip() else None
        source_label = "Existing CityGML"

        if source is None:
            ifc_source, ifc_label = get_active_ifc_for_building(building)
            if ifc_source is None:
                st.error(ifc_label)
                return

            generated_gml = run_root / f"{ifc_source.stem}.gml"
            with st.spinner(f"Preparing CityGML 3.0 from {ifc_label}…"):
                converted, conversion_log = convert_ifc_to_citygml(
                    ifc_source,
                    generated_gml,
                    georef=False,
                )
            if not converted or not generated_gml.exists():
                st.error("Automatic IFC → CityGML 3.0 preparation failed.")
                st.code(
                    conversion_log[-12000:]
                    if conversion_log
                    else "No converter log returned."
                )
                return
            if "skipped" in conversion_log.lower():
                st.caption("Using cached CityGML; IFC geometry conversion was skipped.")
            source = generated_gml
            source_label = f"Auto-converted from IFC · {ifc_label}"

        if not source.exists():
            st.error(f"CityGML source does not exist: {source}")
            return

        started, message = start_simstadt_background(source, workflow, output_dir)
        if started:
            st.success(message)
            st.caption(f"SimStadt input: {source_label} · {source}")
            st.rerun()
        else:
            st.error(message)

    if stop:
        stopped, message = stop_simstadt_background(output_dir)
        (st.success if stopped else st.error)(message)
        st.rerun()

    @st.fragment(run_every="3s")
    def _simstadt_poll():
        _render_simstadt_completed_results(
            output_dir,
            temp,
            co2,
            stats,
            float(threshold),
        )

    _simstadt_poll()

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
