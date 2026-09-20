from __future__ import annotations

import os
import re
from datetime import timedelta

import pandas as pd
import streamlit as st

from temperature_dashboard.charts import (
    co2_time_series,
    coverage_timeline,
    distribution,
    gap_stats,
    observed_carpet,
    temperature_time_series,
)
from temperature_dashboard.db import (
    apply_mapping,
    db_path,
    canonical_building,
    canonical_room,
    exists,
    ensure_database_ready,
    hierarchy,
    mapping_coverage,
    mapping_table,
    metadata_get,
    metadata_set,
    latest_by_room,
    query,
    room_span,
)
from temperature_dashboard.ingest import DEFAULT_CO2_MAPPING, ingest_co2_from_path, ingest_co2_upload, ingest_temperature_excel
from temperature_dashboard.ifc_models import (
    REGISTRY_PATH,
    ROLES,
    delete_floor_plan,
    delete_model,
    floor_plan,
    ingest_model,
    list_floor_plans,
    list_models,
    model_buildings,
    save_floor_plan,
    set_model_active,
    space_mappings,
    update_space_mappings,
    viewer_spaces,
    clear_geometry_cache,
)
from temperature_dashboard.ifc_viewer import build_ifc_figure
from temperature_dashboard.server import fetch_server_data

st.set_page_config(page_title="HFT Indoor Environment Dashboard", page_icon="🌡️", layout="wide")


def _inject_hft_css():
    """Inject HFT Stuttgart branded CSS styling."""
    st.markdown("""
    <style>
    :root {
        --hft-primary: #003366;
        --hft-primary-light: #0066cc;
        --hft-primary-lighter: #e6f0fa;
        --hft-accent-teal: #009999;
        --hft-accent-green: #00a651;
        --hft-accent-amber: #e8a800;
        --hft-accent-red: #cc3333;
        --hft-neutral-dark: #1a2a3a;
        --hft-neutral-grey: #6b7b8c;
        --hft-neutral-light: #d0d8e0;
    }
    .hft-header { background: linear-gradient(135deg, var(--hft-primary) 0%, var(--hft-primary-light) 100%); color: white; padding: 1rem 1.5rem; border-radius: 0.75rem; margin-bottom: 1.5rem; box-shadow: 0 4px 12px rgba(0, 51, 102, 0.2); }
    .hft-header h1 { margin: 0; font-size: 1.75rem; font-weight: 600; }
    .hft-header p { margin: 0.5rem 0 0 0; opacity: 0.9; font-size: 0.95rem; }
    .hft-section-header { color: var(--hft-primary); font-weight: 600; border-bottom: 2px solid var(--hft-primary-lighter); padding-bottom: 0.5rem; margin: 1.5rem 0 1rem 0; }
    .stSidebar .stRadio > div { background: var(--hft-primary-lighter); border-radius: 0.5rem; padding: 0.5rem; }
    .stSidebar .stRadio label { color: var(--hft-primary) !important; font-weight: 500; }
    .stButton > button[kind="primary"] { background: linear-gradient(135deg, var(--hft-primary) 0%, var(--hft-primary-light) 100%) !important; border: none !important; color: white !important; font-weight: 600; }
    .stButton > button[kind="primary"]:hover { box-shadow: 0 4px 12px rgba(0, 51, 102, 0.3) !important; }
    .streamlit-expanderHeader { background-color: var(--hft-primary-lighter) !important; border-radius: 0.5rem !important; color: var(--hft-primary) !important; font-weight: 600 !important; }
    [data-testid="metric-container"] { background: white; border: 1px solid var(--hft-neutral-light); border-radius: 0.5rem; padding: 1rem; box-shadow: 0 1px 3px rgba(0, 51, 102, 0.05); }
    [data-testid="metric-container"] label { color: var(--hft-neutral-grey) !important; font-weight: 600 !important; font-size: 0.75rem !important; text-transform: uppercase; letter-spacing: 0.05em; }
    .stAlert { border-radius: 0.5rem !important; border: none !important; }
    .stAlert[data-baseweb="notification"] { background-color: var(--hft-primary-lighter) !important; color: var(--hft-primary) !important; }
    .stDownloadButton > button { background-color: var(--hft-primary) !important; color: white !important; border: none !important; font-weight: 500; }
    .stDownloadButton > button:hover { background-color: var(--hft-primary-light) !important; }
    .stTabs [data-baseweb="tab-list"] { gap: 0.5rem; }
    .stTabs [data-baseweb="tab"] { background-color: var(--hft-primary-lighter) !important; color: var(--hft-primary) !important; border-radius: 0.5rem 0.5rem 0 0 !important; font-weight: 500 !important; padding: 0.5rem 1rem !important; }
    .stTabs [aria-selected="true"] { background: linear-gradient(135deg, var(--hft-primary) 0%, var(--hft-primary-light) 100%) !important; color: white !important; }
    .stDataFrame { border-radius: 0.5rem; overflow: hidden; border: 1px solid var(--hft-neutral-light); }
    .admin-toolbar { color: var(--hft-neutral-grey); font-size: 0.86rem; padding-top: 0.45rem; }
    .view-scope-card { background: white; border: 1px solid var(--hft-neutral-light); border-left: 4px solid var(--hft-accent-teal); border-radius: 0.6rem; padding: 1rem 1.2rem; margin-bottom: 1rem; }
    </style>
    """, unsafe_allow_html=True)


def secret(name, default=None):
    env = os.getenv(name)
    if env is not None:
        return env
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def admin_unlocked() -> bool:
    return st.session_state.get("admin_ok", False)


def _read_mapping_upload(upload) -> pd.DataFrame:
    if upload.name.lower().endswith(".csv"):
        return pd.read_csv(upload, dtype=str)
    return pd.read_excel(upload, sheet_name=0, dtype=str)


@st.dialog("Dashboard administration", width="large")
def data_manager():
    """Password-protected modal, kept separate from staff navigation."""
    if not admin_unlocked():
        st.caption("Data publishing and sensor/location mappings are restricted to administrators.")
        pwd = st.text_input("Admin password", type="password", key="admin_password")
        if st.button("Unlock administration", type="primary", width="stretch"):
            if pwd and pwd == str(secret("ADMIN_PASSWORD", "CHANGE-ME")):
                st.session_state.admin_ok = True
                st.rerun()
            st.error("Incorrect password")
        return

    status_col, lock_col = st.columns([4, 1])
    status_col.success("Admin unlocked")
    if lock_col.button("Lock", width="stretch"):
        st.session_state.admin_ok = False
        st.rerun()

    upload_tab, mapping_tab, ifc_tab, plan_tab = st.tabs(
        ["Publish data", "Sensor locations", "IFC models", "Floor plans"]
    )
    with upload_tab:
        left, right = st.columns(2)
        with left:
            st.markdown("**Temperature history**")
            tf = st.file_uploader("Excel workbook", type=["xlsx"], key="temp_upload")
            if tf and st.button("Publish Temperature", type="primary", width="stretch"):
                with st.spinner("Importing Temperature workbook…"):
                    res = ingest_temperature_excel(tf, tf.name)
                    _clear_dashboard_caches()
                st.success(f"Loaded {res['rows']:,} readings from {res['sensors']} sensors.")
                st.rerun()
        with right:
            st.markdown("**CO₂ history**")
            cf = st.file_uploader("ZIP, CSV or Excel", type=["zip", "csv", "xlsx", "xls"], key="co2_upload")
            if cf and st.button("Publish CO₂", type="primary", width="stretch"):
                with st.spinner("Importing CO₂ data and applying the HFT mapping…"):
                    res = ingest_co2_upload(cf, cf.name)
                    _clear_dashboard_caches()
                cov = mapping_coverage("CO₂")
                st.success(f"Loaded {res['rows']:,} readings. Mapped {cov['mapped']} of {cov['sensors']} sensors.")
                st.rerun()

        with st.expander("Import CO₂ from an HFT server/network path"):
            st.caption(
                "This reads a directory mounted on the machine running the dashboard, not from your browser. "
                "Use the mapped `W:\\` drive when this Windows dashboard host can access it."
            )
            co2_path = st.text_input(
                "CO₂ CSV directory",
                value="W:\\" if os.name == "nt" else "",
                placeholder=r"W:\ or /mnt/hft-co2",
                help="Use a directory mounted on the machine that hosts this dashboard.",
                key="co2_path",
            )
            path_to_import = co2_path.strip()
            if path_to_import:
                if os.path.isdir(path_to_import):
                    st.caption(f"✅ `{path_to_import}` is available to this dashboard host.")
                else:
                    st.caption(
                        "The folder is checked on the dashboard host. Verify its drive mapping and read permission "
                        "before importing."
                    )
            if st.button("Publish CO₂ from path", width="stretch"):
                if not path_to_import:
                    st.warning("Enter a directory path mounted on the dashboard host.")
                else:
                    try:
                        with st.spinner("Importing CO₂ files from the dashboard host…"):
                            res = ingest_co2_from_path(path_to_import)
                    except FileNotFoundError:
                        st.error(
                            f"The dashboard host cannot find `{path_to_import}`. Verify that the drive or network "
                            "share is mounted for the account running Streamlit, then try again."
                        )
                    except PermissionError:
                        st.error(
                            "The dashboard service cannot read that directory. Verify that its host account "
                            "has permission to access the mounted share."
                        )
                    except OSError:
                        st.error(
                            "The dashboard server could not read the selected directory. Verify that it is "
                            "a mounted folder available to this deployment, then try again."
                        )
                    else:
                        _clear_dashboard_caches()
                        cov = mapping_coverage("CO₂")
                        st.success(
                            f"Loaded {res['ingested']:,} readings. "
                            f"Mapped {cov['mapped']} of {cov['sensors']} sensors."
                        )
                        st.rerun()

    with mapping_tab:
        st.caption("Mappings accept Sensor/ID, Building/Bau, Room/Raum and optional Floor/Geschoss columns. If Floor is omitted, it is inferred from the room label.")
        for kind in ("Temperature", "CO₂"):
            if not exists(kind):
                continue
            with st.expander(f"{kind} sensor locations", expanded=(kind == "Temperature")):
                cov = mapping_coverage(kind)
                st.caption(f"Mapped sensors: {cov['mapped']} / {cov['sensors']} · Unassigned: {cov['unassigned']}")
                mapping = mapping_table(kind)
                download_col, upload_col = st.columns(2)
                download_col.download_button(
                    f"Download {kind} mapping",
                    mapping.to_csv(index=False).encode(),
                    file_name=f"{kind.lower().replace('₂', '2')}_sensor_mapping.csv",
                    mime="text/csv",
                    width="stretch",
                )
                mf = upload_col.file_uploader("Upload CSV or Excel", type=["csv", "xlsx"], key=f"map_{kind}", label_visibility="collapsed")
                if mf and st.button(f"Apply {kind} mapping", key=f"apply_{kind}", type="primary", width="stretch"):
                    n = apply_mapping(kind, _read_mapping_upload(mf))
                    _clear_dashboard_caches()
                    cov2 = mapping_coverage(kind)
                    st.success(f"Updated {n:,} readings; {cov2['mapped']} / {cov2['sensors']} sensors are mapped.")
                    st.rerun()

    with ifc_tab:
        render_ifc_admin()
    with plan_tab:
        render_floor_plan_admin()


def render_ifc_admin():
    st.caption("Upload one model per building or federate Architecture, HVAC, Structure and section models. IfcSpace metadata and geometry are extracted on the server.")
    known_buildings = buildings_available()
    assignment_options = known_buildings + (["Add another building…"] if known_buildings else ["Add another building…"])
    assigned = st.selectbox("Dashboard building", assignment_options, key="ifc_building_assignment")
    if assigned == "Add another building…":
        assigned = st.text_input("New building label", key="ifc_new_building").strip()
    role_col, revision_col = st.columns(2)
    role = role_col.selectbox("Model role", ROLES, key="ifc_role")
    revision = revision_col.text_input("Revision (optional)", key="ifc_revision")
    uploads = st.file_uploader("IFC or IFCZIP files", type=["ifc", "ifczip", "zip"], accept_multiple_files=True, key="ifc_uploads")
    replace_role = st.checkbox("Disable existing active models with the same building and role after each successful import", key="ifc_replace_role")
    if uploads and st.button("Validate, extract and register models", type="primary", width="stretch", key="ifc_publish"):
        if not assigned:
            st.error("Enter or select a building before importing.")
        else:
            progress = st.progress(0, text="Preparing IFC imports…")
            successes = 0
            for index, upload in enumerate(uploads):
                try:
                    progress.progress(index / len(uploads), text=f"Extracting {upload.name}…")
                    result = ingest_model(upload.name, upload.getvalue(), assigned, role, revision, replace_role)
                    _clear_dashboard_caches()
                    st.success(f"{upload.name}: model #{result['id']} · {result['spaces']} spaces · {result['geometry_spaces']} with geometry · {result['schema_name']}")
                    successes += 1
                except Exception as exc:
                    st.error(f"{upload.name}: {exc}")
            progress.progress(1.0, text=f"Completed: {successes} of {len(uploads)} models registered.")

    models = list_models()
    st.markdown("**Registered models**")
    if models.empty:
        st.info("No IFC models have been registered yet.")
        return
    display = models.copy()
    display["size"] = display["size_bytes"].map(lambda value: f"{value / (1024 * 1024):.1f} MB")
    st.dataframe(display.drop(columns=["size_bytes"]), width="stretch", hide_index=True)
    labels = {int(row.id): f"#{row.id} · {row.building} · {row.role} · {row.filename}" for row in models.itertuples()}
    selected_id = st.selectbox("Manage model", list(labels), format_func=labels.get, key="ifc_manage_id")
    selected = models.loc[models["id"] == selected_id].iloc[0]
    action_a, action_b = st.columns(2)
    active_label = "Disable model" if bool(selected["active"]) else "Enable model"
    if action_a.button(active_label, width="stretch", key="ifc_toggle"):
        set_model_active(selected_id, not bool(selected["active"]))
        _clear_dashboard_caches()
        st.rerun()
    confirm_delete = st.checkbox("Confirm permanent deletion of the selected model and extracted geometry", key="ifc_confirm_delete")
    if action_b.button("Delete model", disabled=not confirm_delete, width="stretch", key="ifc_delete"):
        delete_model(selected_id)
        _clear_dashboard_caches()
        st.rerun()

    mappings = space_mappings(str(selected["building"]))
    st.markdown("**IFC space → dashboard location mapping**")
    if mappings.empty:
        st.info("This building has no extracted IfcSpace entities. Architecture models normally provide room spaces.")
        return
    st.caption("Automatic guesses use IfcSpace LongName/Name and the containing IfcBuildingStorey. Edit the three dashboard columns to preserve a manual override.")
    editable = ["dashboard_building", "dashboard_floor", "dashboard_room"]
    disabled = [column for column in mappings.columns if column not in editable]
    edited = st.data_editor(mappings, disabled=disabled, hide_index=True, width="stretch", key=f"ifc_space_editor_{selected['building']}")
    if st.button("Save space mappings", type="primary", width="stretch", key="ifc_save_mappings"):
        count = update_space_mappings(edited)
        _clear_dashboard_caches()
        st.success(f"Saved {count} IFC space mappings.")
        st.rerun()


def render_floor_plan_admin():
    """Manage the reference plan displayed with a selected 3D floor."""
    st.caption(
        "Upload one PDF reference layout per dashboard building and floor. "
        "A new upload replaces the existing plan for that location."
    )
    known_buildings = buildings_available()
    if not known_buildings:
        st.info("Publish sensor data or register an IFC model before assigning a floor plan.")
        return

    building = st.selectbox("Dashboard building", known_buildings, key="floor_plan_building")
    floors = floors_available(building)
    if not floors:
        st.info("No dashboard floors are available for this building yet.")
        return

    floor = st.selectbox("Dashboard floor", floors, key="floor_plan_floor")
    existing = floor_plan(building, floor)
    if existing:
        st.success(f"Current plan: {existing['filename']} ({existing['size_bytes'] / (1024 * 1024):.1f} MB)")
    else:
        st.info("No PDF plan is registered for this building and floor.")

    upload = st.file_uploader(
        "Floor-plan PDF",
        type=["pdf"],
        key="floor_plan_upload",
        help="Use a readable floor layout. The selected building and floor determine where it is displayed.",
    )
    upload_col, delete_col = st.columns(2)
    if upload_col.button(
        "Upload / replace plan",
        type="primary",
        width="stretch",
        disabled=upload is None,
        key="floor_plan_save",
    ):
        try:
            saved = save_floor_plan(upload.name, upload.getvalue(), building, floor)
        except ValueError as exc:
            st.error(str(exc))
        else:
            _clear_dashboard_caches()
            st.success(f"Saved {saved['filename']} for {saved['building']} · {saved['floor']}.")
            st.rerun()

    if delete_col.button(
        "Delete current plan",
        width="stretch",
        disabled=existing is None,
        key="floor_plan_delete",
    ):
        delete_floor_plan(building, floor)
        _clear_dashboard_caches()
        st.success("Deleted the floor-plan PDF.")
        st.rerun()

    plans = list_floor_plans(building)
    if not plans.empty:
        st.markdown("**Registered floor plans**")
        display = plans.drop(columns=["stored_filename"]).copy()
        display["size"] = display.pop("size_bytes").map(lambda value: f"{value / (1024 * 1024):.1f} MB")
        st.dataframe(display, width="stretch", hide_index=True)


def _natural_key(value):
    text = str(value)
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", text)]


def _data_version(kind: str) -> int:
    path = db_path(kind)
    return path.stat().st_mtime_ns if path.exists() else 0


def _ifc_version() -> int:
    return REGISTRY_PATH.stat().st_mtime_ns if REGISTRY_PATH.exists() else 0


@st.cache_data(show_spinner=False)
def _cached_hierarchy(kind: str, version: int) -> pd.DataFrame:
    return hierarchy(kind)


@st.cache_data(show_spinner=False)
def _cached_space_mappings(building: str, version: int) -> pd.DataFrame:
    return space_mappings(building)


@st.cache_data(show_spinner=False)
def _cached_model_buildings(version: int) -> list[str]:
    return model_buildings()


@st.cache_data(show_spinner=False)
def _cached_latest_by_room(kind: str, building: str, floor: str | None, room: str | None, version: int) -> pd.DataFrame:
    return latest_by_room(kind, building, floor=floor, room=room)


def _clear_dashboard_caches() -> None:
    st.cache_data.clear()
    clear_geometry_cache()


def buildings_available() -> list[str]:
    values = set(_cached_model_buildings(_ifc_version()))
    for kind in ("Temperature", "CO₂"):
        if exists(kind):
            values.update(_cached_hierarchy(kind, _data_version(kind))["building"].astype(str).tolist())
    return sorted(values, key=lambda x: (x == "Unassigned", _natural_key(x)))


def floors_available(building: str) -> list[str]:
    mappings = _cached_space_mappings(building, _ifc_version())
    values = set(mappings["dashboard_floor"].dropna().astype(str).tolist()) if not mappings.empty else set()
    for kind in ("Temperature", "CO₂"):
        if exists(kind):
            h = _cached_hierarchy(kind, _data_version(kind))
            values.update(h.loc[h["building"].astype(str) == str(building), "floor"].astype(str).tolist())
    return sorted(values, key=lambda x: (x == "Unassigned", _natural_key(x)))


def rooms_available(kind: str, building: str, floor: str | None = None) -> list[str]:
    if not exists(kind):
        return []
    h = _cached_hierarchy(kind, _data_version(kind))
    mask = h["building"].astype(str) == str(building)
    if floor is not None:
        mask &= h["floor"].astype(str) == str(floor)
    values = h.loc[mask, "room"].astype(str).unique().tolist()
    return sorted(values, key=lambda x: (x == "Unassigned", _natural_key(x)))


def _scope_latest_values(building: str, floor: str | None, room: str | None) -> dict[tuple[str, str], dict]:
    result: dict[tuple[str, str], dict] = {}
    for kind in ("Temperature", "CO₂"):
        data = _cached_latest_by_room(kind, building, floor, room, _data_version(kind))
        if data.empty:
            continue
        data = data.sort_values("timestamp").groupby(["floor", "room"], as_index=False).tail(1)
        for row in data.itertuples(index=False):
            result.setdefault((str(row.floor), str(row.room)), {})[kind] = float(row.value)
    return result


@st.fragment
def render_3d_scope(building: str, floor: str):
    scope = st.sidebar.radio("3D scope", ["Room", "Floor", "Exploded building"], horizontal=False)
    selected_room = None
    if scope == "Room":
        mapped_spaces = _cached_space_mappings(building, _ifc_version())
        if not mapped_spaces.empty:
            mapped_spaces = mapped_spaces.loc[mapped_spaces["dashboard_floor"].astype(str) == str(floor)]
        ifc_rooms = set(mapped_spaces["dashboard_room"].dropna().astype(str)) if not mapped_spaces.empty else set()
        room_options = sorted(
            set(rooms_available("Temperature", building, floor)) | set(rooms_available("CO₂", building, floor)) | ifc_rooms,
            key=_natural_key,
        )
        selected_room = st.sidebar.selectbox("Room", room_options, key="3d_room") if room_options else None
    scope_floor = floor if scope in {"Room", "Floor"} else None
    scope_room = selected_room if scope == "Room" else None

    labels = {
        "Room": f"Building {building} · {floor} · Room {selected_room or 'not selected'}",
        "Floor": f"Building {building} · {floor}",
        "Exploded building": f"Building {building} · all floors",
    }
    st.markdown(f'<div class="view-scope-card"><strong>3D scope:</strong> {labels[scope]}<br><small>Latest observed Temperature and CO₂ values are prepared room-by-room.</small></div>', unsafe_allow_html=True)
    if scope == "Room" and not selected_room:
        st.info("Select a room with available Temperature or CO₂ data.")
        return

    measurement = st.sidebar.radio("Space color", ["Temperature", "CO₂"], key="ifc_measurement")
    storey_gap = st.sidebar.slider("Exploded storey gap", 0.0, 20.0, 5.0, 0.5, disabled=scope != "Exploded building")
    spaces = viewer_spaces(building, scope_floor, scope_room)
    if not spaces:
        st.warning("No active, mapped IfcSpace geometry is available for this scope. An administrator can upload an Architecture IFC and adjust its space mappings.")
        return
    latest_raw = _scope_latest_values(building, scope_floor, scope_room)
    figure = build_ifc_figure(spaces, latest_raw, measurement, scope == "Exploded building", storey_gap)
    plan = floor_plan(building, floor)
    if plan:
        viewer_col, plan_col = st.columns([1.35, 1], gap="medium")
        with viewer_col:
            st.plotly_chart(
                figure,
                width="stretch",
                key=f"ifc_{building}_{scope}_{scope_floor}_{scope_room}_{measurement}",
            )
        with plan_col:
            pdf_data = plan["path"].read_bytes()
            st.markdown(f"#### Reference layout · {floor}")
            st.caption(plan["filename"])
            if scope == "Exploded building":
                st.caption("Reference plan for the selected floor; the 3D model shows all floors.")
            st.pdf(pdf_data, height=580)
            st.download_button(
                "Download floor plan",
                pdf_data,
                file_name=plan["filename"],
                mime="application/pdf",
                width="stretch",
            )
    else:
        st.plotly_chart(
            figure,
            width="stretch",
            key=f"ifc_{building}_{scope}_{scope_floor}_{scope_room}_{measurement}",
        )
        st.info(
            f"No PDF reference layout is registered for {building} · {floor}. "
            "An administrator can add one in ⚙ Admin → Floor plans."
        )
    st.caption("Room IDs and latest values are shown directly on matching IFC spaces. Drag to orbit, Shift-drag to pan, use the wheel to zoom, and click legend entries to toggle federated layers.")


def selected_spans(building: str, temp_room: str | None, co2_room: str | None):
    out = {}
    if temp_room:
        s = room_span("Temperature", building, temp_room)
        if s:
            out["Temperature"] = (pd.Timestamp(s["start"]), pd.Timestamp(s["end"]))
    if co2_room:
        s = room_span("CO₂", building, co2_room)
        if s:
            out["CO₂"] = (pd.Timestamp(s["start"]), pd.Timestamp(s["end"]))
    return out


def choose_span_basis(spans: dict):
    if not spans:
        return None, None, "none"
    starts = [value[0] for value in spans.values()]
    ends = [value[1] for value in spans.values()]
    overlap = (max(starts), min(ends))
    union = (min(starts), max(ends))
    if overlap[0] <= overlap[1]:
        basis = st.sidebar.selectbox(
            "Time window basis",
            ["Common Temperature/CO₂ overlap", "All available dates"],
            help="Common overlap keeps both selected room datasets on the same dates. All available dates includes full history and leaves missing periods empty.",
        )
        return (*overlap, "overlap") if basis.startswith("Common") else (*union, "union")
    return (*union, "union")


def choose_time_range(start: pd.Timestamp, end: pd.Timestamp):
    choice = st.sidebar.radio("Time range", ["24 h", "7 d", "30 d", "1 year", "All", "Custom"])
    if choice == "24 h":
        return max(start, end - timedelta(hours=24)), end, choice
    if choice == "7 d":
        return max(start, end - timedelta(days=7)), end, choice
    if choice == "30 d":
        return max(start, end - timedelta(days=30)), end, choice
    if choice == "1 year":
        return max(start, end - timedelta(days=365)), end, choice
    if choice == "All":
        return start, end, choice
    dates = st.sidebar.date_input(
        "Custom dates",
        value=(start.date(), end.date()),
        min_value=start.date(),
        max_value=end.date(),
    )
    if isinstance(dates, (tuple, list)) and len(dates) == 2:
        return pd.Timestamp(dates[0]), pd.Timestamp(dates[1]) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1), choice
    return start, end, choice


def server_config(kind: str):
    if kind == "Temperature":
        return {
            "url": str(secret("SERVER_API_URL", "") or ""),
            "token": str(secret("SERVER_API_TOKEN", "") or ""),
            "timeout": int(secret("SERVER_TIMEOUT", 10) or 10),
            "fields": {
                "timestamp": str(secret("SERVER_TIMESTAMP_FIELD", "timestamp")),
                "value": str(secret("SERVER_TEMPERATURE_FIELD", "temperature")),
                "sensor": str(secret("SERVER_SENSOR_FIELD", "sensor_id")),
                "building": str(secret("SERVER_BUILDING_FIELD", "building")),
                "room": str(secret("SERVER_ROOM_FIELD", "room")),
            },
        }
    return {
        "url": str(secret("CO2_SERVER_API_URL", "") or ""),
        "token": str(secret("CO2_SERVER_API_TOKEN", "") or ""),
        "timeout": int(secret("CO2_SERVER_TIMEOUT", 10) or 10),
        "fields": {
            "timestamp": str(secret("CO2_SERVER_TIMESTAMP_FIELD", "timestamp")),
            "value": str(secret("CO2_SERVER_VALUE_FIELD", "co2")),
            "sensor": str(secret("CO2_SERVER_SENSOR_FIELD", "sensor_id")),
            "building": str(secret("CO2_SERVER_BUILDING_FIELD", "building")),
            "room": str(secret("CO2_SERVER_ROOM_FIELD", "room")),
        },
    }


def _collect(kind: str, building: str, room: str, start, end, source_mode: str, cfg: dict) -> tuple[pd.DataFrame, str | None]:
    frames = []
    error = None
    if source_mode in ("Historical uploads", "Historical + live") and exists(kind):
        hist = query(kind, building, room, start, end)
        if not hist.empty:
            frames.append(hist)
    if source_mode in ("Live servers", "Historical + live") and cfg["url"]:
        try:
            live = fetch_server_data(cfg["url"], cfg["token"], cfg["timeout"], cfg["fields"])
            if not live.empty:
                if "building" not in live.columns:
                    live["building"] = "Unassigned"
                if "room" not in live.columns:
                    live["room"] = "Unassigned"
                if "sensor" not in live.columns:
                    live["sensor"] = "Live"
                live["building"] = live["building"].map(canonical_building)
                live["room"] = live["room"].map(canonical_room)
                live = live[(live["building"] == building) & (live["room"] == room)].copy()
                live = live[(live["timestamp"] >= start) & (live["timestamp"] <= end)]
                live["secondary_temperature"] = None
                live["humidity"] = None
                live["source"] = "Live server"
                frames.append(live[["timestamp", "sensor", "building", "room", "value", "secondary_temperature", "humidity", "source"]])
        except Exception as exc:
            error = str(exc)
    if not frames:
        return pd.DataFrame(columns=["timestamp", "sensor", "building", "room", "value", "secondary_temperature", "humidity", "source"]), error
    df = pd.concat(frames, ignore_index=True).sort_values("timestamp")
    return df, error


def _metric_rows(df: pd.DataFrame, unit: str):
    q = gap_stats(df)
    gaps = int(q["gaps"].sum()) if not q.empty else 0
    latest = df.sort_values("timestamp").iloc[-1]["value"]
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Latest observed", f"{latest:.1f} {unit}")
    c2.metric("Observed minimum", f"{df['value'].min():.1f} {unit}")
    c3.metric("Observed maximum", f"{df['value'].max():.1f} {unit}")
    c4.metric("Readings", f"{len(df):,}")
    c5.metric("Detected gaps", f"{gaps:,}")


def _detail_section(df: pd.DataFrame, kind: str, key_prefix: str, is_technical: bool = False):
    if df.empty:
        return
    with st.expander(f"{kind} details · carpet, coverage, distribution, quality and export", expanded=False):
        sensors = sorted(df["sensor"].astype(str).dropna().unique(), key=_natural_key)
        sensor = st.selectbox("Sensor for observed carpet plot", sensors, key=f"{key_prefix}_carpet_sensor")
        
        if is_technical:
            st.caption("The carpet uses the actual reading nearest the middle of each hour. Empty hours stay empty; no hourly average or interpolation is used.")
        
        st.plotly_chart(observed_carpet(df, kind, sensor), width="stretch", key=f"{key_prefix}_carpet")

        st.markdown("**Measurement coverage / outages**")
        st.plotly_chart(coverage_timeline(df), width="stretch", key=f"{key_prefix}_coverage")

        st.markdown("**Observed-value distribution**")
        st.plotly_chart(distribution(df, kind), width="stretch", key=f"{key_prefix}_distribution")

        if is_technical:
            st.markdown("**Data quality and gaps by sensor**")
            quality = gap_stats(df)
            st.dataframe(quality, width="stretch", hide_index=True)

        st.markdown("**Raw observations**")
        st.dataframe(df.tail(5000), width="stretch", hide_index=True)
        st.download_button(
            f"Download filtered {kind} CSV",
            df.to_csv(index=False).encode(),
            file_name=f"{kind.lower().replace('₂', '2')}_filtered.csv",
            mime="text/csv",
            key=f"{key_prefix}_download",
        )



def ensure_packaged_co2_mapping():
    """Apply the provided HFT CO2 location workbook once to an existing CO2 database."""
    if not exists("CO₂") or not DEFAULT_CO2_MAPPING.exists():
        return
    if metadata_get("CO₂", "packaged_hft_mapping") == "202510":
        return
    try:
        mapping = pd.read_csv(DEFAULT_CO2_MAPPING, dtype=str)
        apply_mapping("CO₂", mapping)
        metadata_set("CO₂", "packaged_hft_mapping", "202510")
    except Exception:
        # The admin mapping control remains available if an existing DB cannot be updated.
        pass


@st.cache_resource(show_spinner="Optimizing dashboard database…")
def _prepare_databases() -> bool:
    for kind in ("Temperature", "CO₂"):
        ensure_database_ready(kind)
    return True


def render_dashboard():
    _prepare_databases()
    ensure_packaged_co2_mapping()
    
    # Inject HFT branded CSS
    _inject_hft_css()
    
    # Branded header
    st.markdown("""
    <div class="hft-header">
        <h1>🌡️ HFT Indoor Environment Dashboard</h1>
        <p>Temperature + CO₂ monitoring · Building → Floor → Room · Actual observations with visible gaps</p>
    </div>
    """, unsafe_allow_html=True)

    admin_text, admin_action = st.columns([8, 1])
    admin_text.markdown('<div class="admin-toolbar">Staff dashboard · administrative tools are kept separate from viewing controls.</div>', unsafe_allow_html=True)
    if admin_action.button("⚙ Admin", width="stretch"):
        data_manager()

    if not exists("Temperature") and not exists("CO₂") and not _cached_model_buildings(_ifc_version()):
        st.info("No indoor dataset or active IFC model has been initialized. Open **⚙ Admin** to publish readings and/or IFC models.")
        return

    buildings = buildings_available()
    if not buildings:
        st.warning("No Building locations are available.")
        return
    st.sidebar.markdown("### Location")
    building = st.sidebar.selectbox("Building", buildings)
    floors = floors_available(building)
    if not floors:
        st.warning("No floor locations can be derived for this Building. An administrator can add Floor/Geschoss in the sensor mapping.")
        return
    floor = st.sidebar.selectbox("Floor", floors)
    ui_mode = st.sidebar.radio("Dashboard View", ["Normal", "Technical", "3D"], horizontal=False)

    if ui_mode == "3D":
        render_3d_scope(building, floor)
        return

    temp_rooms = rooms_available("Temperature", building, floor)
    co2_rooms = rooms_available("CO₂", building, floor)
    temp_room = st.sidebar.selectbox("Temperature room", temp_rooms, key="temp_room") if temp_rooms else None
    co2_room = st.sidebar.selectbox("CO₂ room", co2_rooms, key="co2_room") if co2_rooms else None

    if not temp_rooms:
        st.sidebar.caption("No Temperature rooms on this Floor.")
    if not co2_rooms:
        st.sidebar.caption("No mapped CO₂ rooms on this Floor.")

    if temp_room:
        ht = _cached_hierarchy("Temperature", _data_version("Temperature"))
        sensors = ht[(ht["building"].astype(str) == str(building)) & (ht["room"].astype(str) == str(temp_room))]["sensor"].astype(str).tolist()
        if sensors:
            st.sidebar.caption("Temperature sensor IDs: " + ", ".join(sorted(sensors, key=_natural_key)))
    if co2_room:
        hc = _cached_hierarchy("CO₂", _data_version("CO₂"))
        sensors = hc[(hc["building"].astype(str) == str(building)) & (hc["room"].astype(str) == str(co2_room))]["sensor"].astype(str).tolist()
        if sensors:
            st.sidebar.caption("CO₂ sensor IDs: " + ", ".join(sorted(sensors, key=_natural_key)))

    spans = selected_spans(building, temp_room, co2_room)
    span_start, span_end, span_basis = choose_span_basis(spans)
    if span_start is None:
        st.warning("The selected Building has no observations.")
        return
    start, end, range_choice = choose_time_range(span_start, span_end)
    st.sidebar.caption(f"Window: {start:%d %b %Y %H:%M} → {end:%d %b %Y %H:%M}")

    temp_cfg, co2_cfg = server_config("Temperature"), server_config("CO₂")
    live_available = bool(temp_cfg["url"] or co2_cfg["url"])
    if live_available:
        source_mode = st.sidebar.selectbox("Indoor data source", ["Historical uploads", "Historical + live", "Live servers"], index=1)
    else:
        source_mode = "Historical uploads"
        st.sidebar.caption("Live server APIs are not configured yet.")

    reference = float(st.sidebar.number_input("CO₂ reference line (ppm)", min_value=400.0, max_value=5000.0, value=1000.0, step=50.0))

    auto_refresh = False
    refresh_seconds = 30
    if live_available and source_mode != "Historical uploads":
        auto_refresh = st.sidebar.checkbox("Auto-refresh live servers", value=True)
        refresh_seconds = st.sidebar.selectbox("Refresh every", [15, 30, 60, 300], index=1, format_func=lambda x: f"{x} seconds" if x < 60 else f"{x // 60} minutes")
        if st.sidebar.button("Refresh now", width="stretch"):
            st.rerun()

    def panels():
        temp_df, temp_error = (_collect("Temperature", building, temp_room, start, end, source_mode, temp_cfg) if temp_room else (pd.DataFrame(), None))
        co2_df, co2_error = (_collect("CO₂", building, co2_room, start, end, source_mode, co2_cfg) if co2_room else (pd.DataFrame(), None))

        if temp_error:
            st.warning(f"Temperature live server unavailable: {temp_error}")
        if co2_error:
            st.warning(f"CO₂ live server unavailable: {co2_error}")

        is_technical = ui_mode == "Technical"
        
        st.markdown(f'<div class="hft-section-header">Building {building}</div>', unsafe_allow_html=True)
        room_summary = []
        if temp_room:
            room_summary.append(f"Temperature room **{temp_room}**")
        if co2_room:
            room_summary.append(f"CO₂ room **{co2_room}**")
        st.markdown(" · ".join(room_summary))
        
        if is_technical:
            st.caption(
                "Time range changes only the visible window. Main lines use actual measurements. "
                "For long ranges, drawing is reduced with a shape-preserving selection of real observations—never daily/weekly/monthly means. "
                "Detected outages are shown as line breaks and are not interpolated."
            )
        else:
            st.caption("Data shown from historical uploads. Time range changes the visible window only.")

        st.markdown('<div class="hft-section-header">Temperature</div>', unsafe_allow_html=True)
        if temp_df.empty:
            st.info("No indoor Temperature readings are available for the selected Temperature room and time window.")
        else:
            _metric_rows(temp_df, "°C")

        if not temp_df.empty:
            st.plotly_chart(temperature_time_series(temp_df), width="stretch", key="temperature_main")
        
        if is_technical:
            _detail_section(temp_df, "Temperature", "temperature", True)

        st.divider()
        st.markdown('<div class="hft-section-header">CO₂</div>', unsafe_allow_html=True)
        if co2_df.empty:
            st.info("No mapped CO₂ readings are available for the selected CO₂ room and time window.")
        else:
            _metric_rows(co2_df, "ppm")
            above = int((co2_df["value"] > reference).sum())
            pct = 100.0 * above / len(co2_df) if len(co2_df) else 0.0
            
            if is_technical:
                st.caption(f"User-selected reference: {reference:g} ppm · {above:,} readings ({pct:.1f}%) are above it in this window. This is an analysis reference, not a health classification.")
            else:
                st.caption(f"Reference threshold: {reference:g} ppm · Values above indicate need for ventilation.")
            
            st.plotly_chart(co2_time_series(co2_df, reference), width="stretch", key="co2_main")
        
        if is_technical:
            _detail_section(co2_df, "CO₂", "co2", True)

    if live_available and source_mode != "Historical uploads" and auto_refresh and hasattr(st, "fragment"):
        st.fragment(run_every=f"{refresh_seconds}s")(panels)()
    else:
        panels()


render_dashboard()
