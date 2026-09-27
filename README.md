# HFT Temperature & CO₂ Dashboard

The **HFT Temperature & CO₂ Dashboard** is a Streamlit web application for monitoring, analyzing, and visualizing indoor climate data (Temperature and CO₂) across HFT buildings, floors, and rooms.

The application combines sensor analytics with IFC-based building context and an optional **SimStadt energy-analysis workflow**. The dashboard is designed to keep interactive Streamlit use responsive while longer simulation jobs run separately.

## 🚀 Key Functions & Features

### 1. Data Visualization & Analytics

- **Unified Dashboard**: View Temperature and CO₂ metrics with Normal and Technical viewing modes.
- **Hierarchical Navigation**: Filter by **Building → Floor → Room**.
- **Time-Series Charts**: Interactive Temperature and CO₂ timelines with comfort/IAQ reference bands and visible data gaps.
- **Carpet Plots (Heatmaps)**: Inspect recurring daily/weekly patterns by day and hour.
- **Distribution & Gap Statistics**: Review distributions, completeness, and sensor coverage.
- **Sensor-derived conditions for simulation**:
  - mean and percentile Temperature,
  - mean and P95 indoor CO₂,
  - an occupancy proxy based on a configurable CO₂ threshold,
  - an explicitly labelled CO₂-decay ACH diagnostic proxy.

> **Important:** the ACH value is a diagnostic proxy from observed CO₂ decay. It is **not** presented as a DIN-compliant or native SimStadt ventilation calculation.

### 2. 3D IFC Model Viewer

- **Interactive 3D Context**: Renders mapped spaces from uploaded IFC/IFCZIP BIM models.
- **Climate Coloring**: Colors mapped rooms from the latest Temperature/CO₂ observations.
- **Viewer Scopes**: Room, Floor, or Exploded building views.
- **Layer Management**: Toggle registered model roles such as Architecture, HVAC, Structure, and Electrical.
- **Space Mapping**: Associate IFC spaces with dashboard Building/Floor/Room identifiers.

### 3. 2D Floor Plan References

- **PDF Layouts**: Upload and view per-floor 2D reference plans.
- **Side-by-Side Context**: Compare the selected floor's IFC-based 3D view with the native PDF floor plan.

### 4. Admin & Data Management

- **Password-Protected Admin Panel**: Manage uploads, mappings, and configuration.
- **Data Ingestion**: Upload Temperature Excel files and CO₂ CSV/data files.
- **Sensor Mapping**: Map raw sensor IDs to physical Building/Floor/Room locations.
- **IFC Model Management**: Upload `.ifc` or `.ifczip` models, assign buildings and discipline/role metadata, and maintain active model selection.
- **Floor Plan Management**: Assign reference PDF floor plans to buildings and floors.

### 5. SimStadt Energy & Environmental Sandbox

The dashboard now contains a real SimStadt integration under the **Dashboard View / SimStadt Sandbox** workflow.

The current simulation scope is intentionally limited to:

| Workflow | Purpose |
|---|---|
| **Heat Demand** | Annual/building heating-demand analysis |
| **Hourly Heat Demand** | Time-resolved heat-demand analysis |
| **Environmental / CO₂ Analysis** | Energy/environmental analysis including CO₂ emissions |

**PV potential** and **Refurbishment** workflows are not part of the current dashboard scope.

#### Automatic IFC → CityGML 3.0 conversion

When no CityGML input is supplied manually, the Sandbox:

1. Finds the active **Architecture IFC** registered for the selected building.
2. Converts that IFC automatically to **CityGML 3.0**.
3. Reuses the generated CityGML when the IFC has not changed.
4. Passes the resulting CityGML model to SimStadt.

The conversion uses the **TUM-GIS IFC-to-CityGML 3.0 converter**. It can be provided as a local executable through `IFC2CITYGML_COMMAND` or discovered through Docker.

The generated CityGML is kept in the temporary SimStadt run workspace rather than replacing the source IFC.

#### SimStadt backend discovery

The application checks for a SimStadt backend in this order:

- `SIMSTADT_COMMAND`
- local `simstadt` / `simstadtpy` executable
- `SIMSTADT_EXECUTABLE` / `SIMSTADT_HOME`
- Docker

This means SimStadt is **not bundled in the Python requirements**. A separate local installation or Docker runtime is required to execute the actual simulation.

#### Non-blocking simulation execution

Long SimStadt calculations are launched as a background process instead of blocking the Streamlit request.

The Sandbox provides:

- **Start SimStadt in background**
- live worker-log polling
- **Stop** control
- job-state tracking
- automatic detection of completed output files
- result-table rendering
- downloadable result CSVs

Simulation state is recorded in the temporary run directory using a job JSON file and worker log.

#### Automatic sensor validation

After a completed simulation, the dashboard extracts available SimStadt CSV outputs and presents:

- SimStadt headline heating-demand values when available,
- observed HFT Temperature statistics,
- observed HFT CO₂ statistics,
- occupancy proxy,
- ACH-decay diagnostic proxy,
- direct hourly MAE/RMSE/bias/R² metrics **only when the SimStadt output contains a comparable physical variable**.

The validation deliberately does **not** compare indoor CO₂ concentration (ppm) directly with SimStadt CO₂ emissions, because they represent different physical quantities.

For direct energy validation, measured heating/energy consumption data (for example kWh by hour or year) should be added to the project.

## 📖 User Guide

### 1. Navigating to a Room

1. Select a **Building**.
2. Select a **Floor**.
3. Select a **Room**.

All dashboard charts, IFC context, and Sandbox selections follow the current location.

### 2. Normal vs. Technical View

- **Normal View**: Focuses on quick comfort and IAQ checks.
- **Technical View**: Shows detailed time series, heatmaps, distributions, and statistics.

### 3. Using the 3D Model

Open the **3D** view to inspect the selected building/floor/room in IFC context.

Rooms with valid IFC space geometry can be colored using the latest sensor observations. Federated IFC files can be registered as separate model-role layers.

### 4. Running the SimStadt Sandbox

1. Select the required **Building / Floor / Room**.
2. Open the **SimStadt Sandbox** view.
3. Review the detected IFC and SimStadt backend.
4. Either provide an existing CityGML path or let the Sandbox automatically convert the active Architecture IFC.
5. Select:
   - Heat Demand,
   - Hourly Heat Demand, or
   - Environmental / CO₂ Analysis.
6. Start the simulation.
7. The simulation continues in the background while the Streamlit page remains responsive.
8. Review SimStadt outputs and the automatic sensor-validation section.

### 5. CityGML Input

A manually supplied CityGML file should be accessible from the same execution host/container that runs the dashboard and SimStadt.

The Sandbox accepts an existing CityGML path or can generate one automatically from the active Architecture IFC.

## 🛠 Installation & Local Development

### Python / Streamlit

Recommended local setup:

```powershell
git clone https://github.com/ealins/HFT-Temp-Dashboard.git
cd HFT-Temp-Dashboard

py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements.txt

streamlit run app.py
```

Then open:

```text
http://localhost:8501
```

### Docker

Build and start the dashboard:

```bash
docker compose up --build
```

Then open:

```text
http://localhost:8501
```

The `./data:/app/data` volume persists dashboard databases, mappings, IFC models, and extracted model geometry.

> **Docker note for SimStadt:** the dashboard image itself does not install SimStadt. The SimStadt Sandbox needs a SimStadt-capable execution environment and access to Docker or a local SimStadt executable. On a self-hosted Docker/Windows setup, the Sandbox can call the Docker engine from the dashboard host.

## 🔧 Optional Environment Variables

The Sandbox supports explicit backend configuration when automatic discovery is not sufficient:

```text
SIMSTADT_COMMAND=<path or command for SimStadt>
SIMSTADT_EXECUTABLE=<path to SimStadt executable>
SIMSTADT_HOME=<SimStadt installation path>

IFC2CITYGML_COMMAND=<path or command for IFC-to-CityGML converter>
```

The documented Docker image used by the IFC-to-CityGML integration is:

```text
ghcr.io/tum-gis/ifc-to-citygml3:latest
```

## 📁 Technical Architecture & Workflow

```text
HFT Sensors
   │
   ├── Temperature ───────────────┐
   └── Indoor CO₂ ────────────────┤
                                  ▼
                         Streamlit Dashboard
                                  │
                     ┌────────────┴────────────┐
                     │                         │
                     ▼                         ▼
                IFC Registry              Sensor Analytics
                     │                         │
              Active Architecture IFC         ├─ Occupancy proxy
                     │                         ├─ CO₂ decay diagnostic
                     ▼                         └─ Validation conditions
              IFC → CityGML 3.0
                     │
                     ▼
                SimStadt
                     │
          ┌──────────┼─────────────┐
          ▼          ▼             ▼
      HeatDemand  HourlyHeatDemand  EnvironmentalAnalysis
          │          │             │
          └──────────┴─────────────┘
                     ▼
              CSV / Summary Results
                     │
                     ▼
             Automatic validation
```

### IFC processing

IfcOpenShell extracts:

- `IfcProject`
- `IfcBuilding`
- `IfcBuildingStorey`
- `IfcSpace`

and stores extracted space geometry for the 3D dashboard viewer.

Persistent IFC data is stored under:

```text
data/ifc_models/
data/ifc_models.db
```

The default per-file upload limit is 500 MB; adjust Streamlit upload settings for larger models.

Architecture IFCs normally need modeled `IfcSpace` solids for room-level visualization. Non-architecture federated files can be registered and displayed as layers when they contain usable `IfcSpace` geometry.

### Simulation workspace

SimStadt runs use a temporary workspace beneath the operating system's temporary directory:

```text
hft_simstadt_runs/
  <building>/
    <floor>/
      <generated-or-supplied CityGML>
      output/
      simstadt_job.json
      simstadt_worker.log
```

The generated CityGML is refreshed when its source IFC is newer.

### Git Repository

This repository tracks the production code on the `main` branch.

Before running a local checkout after repository-side updates:

```powershell
git switch main
git pull origin main
```

## ⚠️ Important Scope & Validation Notes

- SimStadt requires a valid CityGML model and its required simulation inputs/assumptions; IFC → CityGML conversion alone does not guarantee that every energy-model parameter is fully populated.
- Sensor Temperature and indoor CO₂ data are useful for boundary-condition diagnostics and validation, but they are not a substitute for all native SimStadt weather, usage, construction, and energy-meter inputs.
- Indoor CO₂ is measured in **ppm**. Environmental-analysis CO₂ from SimStadt represents **emissions**, so they must not be treated as the same variable.
- For a robust measured-vs-simulated energy comparison, add measured heating/energy consumption data.
- Long-running background execution is intended primarily for self-hosted/local environments where the dashboard process is allowed to launch child processes. Managed Streamlit hosting may require a separate worker service instead.

## 📚 Related Technical Resources

- **TUM-GIS IFC → CityGML 3.0**: https://github.com/tum-gis/ifc-to-citygml3
- **SimStadt**: https://simstadt.hft-stuttgart.de/
- **If you use the Docker-based IFC converter**, ensure Docker is available to the dashboard host and that the generated CityGML output directory is writable.

## 📝 Upgrade / Existing Data

Keep your existing:

- `.streamlit/secrets.toml`
- `data/*.db`
- `data/ifc_models/`

when updating an existing deployment, unless you intentionally want to replace the stored configuration or model registry.

If no representative project IFC is supplied with an upgrade, validate coordinate alignment, space naming, model geometry, SimStadt input completeness, and execution performance with actual HFT models before production rollout.
