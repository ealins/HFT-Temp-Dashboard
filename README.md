# HFT Temperature & CO₂ Dashboard

A web-based dashboard for monitoring, exploring, and analysing indoor **Temperature** and **CO₂** conditions across buildings, floors, and rooms.

The dashboard combines sensor data with **IFC building models**, interactive 3D room context, floor plans, a room-level **Room Analysis** (ventilation, occupancy, heat balance), and optional **SimStadt** energy analysis that runs in the terminal.

## Features

### Indoor Climate Monitoring

- Monitor Temperature and indoor CO₂ for individual rooms, floors, or buildings.
- Navigate through **Building → Floor → Room**.
- Explore interactive time-series charts.
- Identify recurring patterns using daily/hourly heatmaps.
- Review basic statistics, distributions, and data gaps.
- Use configurable comfort and indoor-air-quality reference thresholds.

### 3D Building Model

Upload and explore IFC/IFCZIP building models directly in the dashboard.

- View building spaces in 3D.
- Navigate by room, floor, or building.
- Inspect room names and mapped locations.
- Colour rooms using recent sensor observations.
- Manage multiple model layers such as Architecture, HVAC, Structure, and Electrical.

### 2D Floor Plans

Upload PDF floor plans and associate them with buildings and floors.

The dashboard can show the floor plan together with the corresponding 3D model to provide both 2D and 3D spatial context.

### Data Management

An administrative interface allows authorised users to:

- Upload Temperature and CO₂ datasets.
- Map sensor identifiers to Building/Floor/Room locations.
- Register IFC/IFCZIP building models.
- Configure model roles and active models.
- Associate PDF floor plans with buildings and floors.

## Room Analysis

The **Room Analysis** view works at room level from sensor data and the IFC geometry. It does not use SimStadt and never opens the IFC while the dashboard runs.

| Tab | What it gives |
|---|---|
| **Occupancy & ventilation** | Hours above the CO₂ limit, occupied-hours proxy, and an air-change rate fitted from CO₂ decay after people leave |
| **Heat balance** | Heat-loss coefficient per element (external walls, windows, doors, roof, ground floor, atrium side, ventilation) and heat loss in kWh over the chosen window |
| **Compare rooms** | Floor or building table: mean temperature, share of time above/below comfort limits, hours above 1000 ppm, window share and main facade, transmission UA |
| **Ventilation vs heat** | Steady-state CO₂ and ventilation heat for different air-change rates, the air-change rate needed to stay under the CO₂ limit, and the effect of heat recovery |

How it works:

- Room areas, volumes, external wall and window areas and facade orientations come from the IFC. They are extracted once per building into `data/ifc_models/<building>/envelope.json`.
- External walls are found from geometry: a room edge counts as external when it faces outside the storey footprint, or carries a window. Edges facing an enclosed atrium are listed separately.
- The IFC contains no usable U-values (only Revit defaults), so U-values are **assumptions**. Choose a construction standard and edit them in the view.
- Indoor temperature comes from the room's temperature sensor or, if none exists, from the CO₂ sensor's own thermometer. Outdoor temperature comes from the local DWD data in `data/outdoor.db` (Stuttgart-Echterdingen airport by default, see below), otherwise from Open-Meteo (needs internet), otherwise from a fixed value.
- The analysis window (24 h / 7 days / 31 days) ends at the dashboard's end, or at the room's last reading if that is earlier.

> **Limits:** the model is steady-state and assumes neighbouring rooms have the same temperature. Use it to compare rooms and scenarios, not as a calibrated prediction. CO₂ sensors that read below outdoor level (calibration drift) are flagged because they make the results less reliable.

Load outdoor temperature once (and again from time to time to add new hours). It downloads hourly DWD open data for the station, looked up by name, and stores it in `data/outdoor.db`:

~~~text
python scripts\ingest_outdoor.py
python scripts\ingest_outdoor.py --station "Stuttgart (Schnarrenberg)"
python scripts\ingest_outdoor.py --file C:\path\stundenwerte_TU_04931_akt.zip
~~~

The airport station is on the Filder plateau, roughly 100 m higher and a few kilometres from the city, so it is typically slightly colder than the campus. The Schnarrenberg station is closer to the city centre. `--file` imports a zip you downloaded yourself when the script cannot reach DWD.

Extract the envelope once per building (about a minute, reads the IFC a single time):

~~~text
python scripts\extract_room_envelope.py --building 1
~~~

## Grafana (fast charts)

The sensor databases are large (CO₂: ~10 M rows, text timestamps), so long ranges are slow in the Streamlit charts.
Grafana reads a compact copy built for fast time-range queries.

```powershell
python scripts\build_grafana_db.py     # builds data\grafana\hft_sensors.db + grafana\dashboards\hft-rooms.json (a few minutes, re-run after new data)
cd grafana
docker compose up -d                  # http://localhost:3000  (admin / admin), dashboard "HFT indoor climate"
```

- The copy stores integer unix-second timestamps and hourly summaries; ranges over 7 days use hourly means.
- Timestamps are local clock times stored as UTC and the dashboard uses the UTC time zone, so clock labels match the readings.
- Outdoor temperature (DWD) is overlaid if `data/outdoor.db` exists (`python scripts\ingest_outdoor.py`).
- Uses the `frser-sqlite-datasource` plugin (installed by the compose file). The database file must not be named `grafana.db`.
- Without Docker: install Grafana OSS for Windows, run `grafana cli plugins install frser-sqlite-datasource`, then copy `grafana\provisioning` and `grafana\dashboards` into its config and point the data source path to `data\grafana\hft_sensors.db`.

## SimStadt Analysis

SimStadt is **never run inside Streamlit**. It runs from the terminal, so a slow or failing simulation cannot freeze the dashboard. The **SimStadt Sandbox** view only reads the finished results.

~~~text
python scripts\run_simstadt_light.py --box-only
python scripts\run_simstadt_light.py --apps heat
python scripts\run_simstadt_light.py --apps heat,refurb,pv
~~~

`--box-only` builds the model and shows it in the Sandbox view in a few seconds without starting SimStadt. Use it as a quick check first.

### Light applications

| App | Description | Weight |
|---|---|---|
| `heat` | Monthly heat demand and KPIs | Light |
| `refurb` | Same, after full refurbishment, to show the savings | Light |
| `pv` | Rooftop PV yield | Light to medium |

No shadow simulation is run. Each app runs in its own low-priority process with a Java memory cap (default 2 GB), a timeout (default 15 minutes), and the whole process tree is stopped if it hangs.

### Scope

| Option | Simulates |
|---|---|
| default | the room selected in the dashboard |
| `--rooms 306,307 --floor "Floor 3"` | named rooms |
| `--scope floor` | the whole selected floor |
| `--scope building` | the whole building |

SimStadt works on whole buildings. A single room becomes a standalone box with outside walls and a roof on every side, so its numbers overstate real demand. Use a single room to compare scenarios (as built against refurbished), and use `--scope floor` or `--scope building` for more realistic losses. For room-level answers use **Room Analysis**.

### Model input

The runner does not need the IFC → CityGML conversion. It builds a simple LoD2 block (ground, walls, flat roof) from the stored IFC room geometry, places it at the HFT location (EPSG:25832) and passes it to SimStadt through its Python API. A different CityGML file can be supplied with `--citygml`.

Results are written to `simstadt_sandbox/outputs/<building>/light/results.json`. The Sandbox view shows the model outline, KPI tables, monthly demand and refurbishment savings, and refreshes itself while a run is in progress.

### Sensor data and simulation

Measured Temperature and CO₂ for the selected room are shown in the Sandbox view as context only. The SimStadt results are not calibrated against them.

> **Important:** indoor CO₂ concentration is measured in **ppm**, while environmental CO₂ results from energy analysis represent emissions. They are different physical quantities and should not be compared as the same variable.

## Getting Started

### Requirements

For the basic dashboard:

- Python 3.12 recommended
- A modern web browser

For IFC functionality:

- IFC/IFCZIP building models
- IFC files containing usable `IfcSpace` information for room-level visualisation

For Room Analysis:

- The room envelope file, created once per building with `scripts/extract_room_envelope.py` (uses `ifcopenshell`, already in `requirements.txt`)

For SimStadt analysis (terminal):

- The `simstadt` Python package in the same environment
- Java 17 or newer
- The SimStadt application, installed by `simstadt --install` (it goes to the Desktop), or its folder given in a `.env` file as `SIMSTADT_FOLDER=...`
- Internet access, because weather data defaults to PVGIS
- INSEL, which the SimStadt documentation lists for the full workflow environment

The runner checks these before starting and prints what is missing.

### SimStadt local runtime

The SimStadt Python package can download and install the SimStadt application locally.

Run once:

~~~text
powershell -ExecutionPolicy Bypass -File .\\scripts\\setup_simstadt.ps1
~~~

Alternatively run `simstadt --install` in the activated environment.

### Optional: IFC → CityGML converter

`scripts/setup_ifc2citygml.ps1` sets up the local TUM-GIS IFC → CityGML 3.0 converter under `.tools/`. The light SimStadt runner does not need it. It is only useful when a full-building CityGML model is wanted.

### Installation

Clone the repository:

~~~text
git clone https://github.com/ealins/HFT-Temp-Dashboard.git
cd HFT-Temp-Dashboard
~~~

Create a Python environment:

**Windows / PowerShell**

~~~text
py -3.12 -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
~~~

**Linux / macOS**

~~~text
python3.12 -m venv .venv
source .venv/bin/activate
~~~

Install the dependencies:

~~~text
python -m pip install --upgrade pip
pip install -r requirements.txt
~~~

Start the dashboard:

~~~text
streamlit run app.py
~~~

Open:

~~~text
http://localhost:8501
~~~

## Using the Dashboard

### 1. Select a Location

Choose:

1. Building
2. Floor
3. Room

The selected location controls the sensor charts, statistics, IFC context, and available analysis options.

### 2. Explore Indoor Climate

Use the Temperature and CO₂ views to:

- inspect historical conditions,
- identify peaks and unusual periods,
- examine daily and hourly patterns,
- evaluate data completeness,
- compare rooms within a building.

### 3. Explore the Building Model

Open the 3D view to inspect the selected area in its BIM/IFC context.

For best room-level results, IFC models should contain properly defined `IfcSpace` elements with meaningful room/storey information.

### 4. Add Floor Plans

Upload a PDF floor plan and assign it to the appropriate building and floor. The floor plan can then be used as a 2D reference alongside the 3D model.

### 5. Analyse a Room

Open **Room Analysis** in the sidebar, choose a room and a window (24 h / 7 days / 31 days), and use the four tabs. Adjust the U-values and other assumptions under **Assumptions**.

### 6. Run SimStadt Analysis

1. Open **SimStadt Sandbox** once and pick the room in the dashboard. The selection is saved for the terminal runner.
2. In the VS Code terminal, from the project folder, run `python scripts\run_simstadt_light.py --box-only`, then `--apps heat` (or `--apps heat,refurb,pv`).
3. Watch the **SimStadt Sandbox** view: it shows the status and fills in the results when the run finishes.

The dashboard stays responsive because the simulation runs in a separate terminal process.

## Data Preparation

### Temperature Data

Temperature data should contain timestamps and measured values and should be associated with the relevant sensor/location mapping.

For meaningful temporal analysis, regular or sufficiently frequent observations are recommended.

### CO₂ Data

CO₂ data should contain timestamps and measured indoor CO₂ concentration values in **ppm**.

The dashboard can use these observations to identify periods of elevated CO₂ and derive a simple occupancy indicator.

### IFC Models

The dashboard supports IFC-based building context and room visualisation.

For useful room-level mapping, IFC models should include:

- `IfcProject`
- `IfcBuilding`
- `IfcBuildingStorey`
- `IfcSpace`

Consistent storey and room naming makes sensor-to-model mapping easier.

## Docker

The dashboard can also be run with Docker:

~~~text
docker compose up --build
~~~

Then open:

~~~text
http://localhost:8501
~~~

The project stores persistent dashboard data in the `data/` directory.

## Typical Use Cases

The dashboard can support:

- indoor climate monitoring,
- thermal-condition assessment,
- indoor-air-quality analysis,
- sensor quality and data-completeness checks,
- room-by-room building analysis,
- BIM-assisted facility management,
- measured-vs-simulated building analysis,
- building energy and environmental studies,
- research and teaching demonstrations involving BIM, sensors, and urban/building analysis.

## Outputs

Depending on the enabled functions, users can obtain:

- interactive Temperature charts,
- interactive CO₂ charts,
- heatmaps,
- room and building statistics,
- 3D IFC visualisation,
- floor-plan references,
- room-level ventilation, occupancy and heat-balance results,
- room comparison tables,
- ventilation-scenario estimates (CO₂ against ventilation heat),
- SimStadt heat-demand, refurbishment and rooftop-PV results.

## Data and Privacy

The dashboard is intended to operate on building and sensor datasets supplied by the deployment owner.

When using real building or occupancy-related data, ensure that the dataset and deployment comply with the applicable institutional, contractual, and data-protection requirements.

## Third-Party Attribution

This repository includes optional setup support for the open-source **TUM-GIS IFC → CityGML 3.0** project, which converts IFC building models into CityGML 3.0. The light SimStadt runner does not use it.

**Source repository:** https://github.com/tum-gis/ifc-to-citygml3

The IFC-to-CityGML conversion capability in this dashboard was adapted from that TUM-GIS repository. The upstream project is developed by the **TUM-GIS** team and provides the IFC-to-CityGML 3.0 conversion functionality used here. Please refer to the upstream repository for its authorship, license, documentation, and original implementation.

This dashboard does not claim ownership of the upstream IFC-to-CityGML converter. Any use, redistribution, or modification of that component remains subject to the upstream project's applicable license and attribution requirements.

## Technical Resources

- **TUM-GIS IFC → CityGML 3.0:** https://github.com/tum-gis/ifc-to-citygml3
- **SimStadt:** https://simstadt.hft-stuttgart.de/

## Project Structure

The main application is started with:

~~~text
app.py
~~~

Core functionality is organised into modules for:

- dashboard and sensor analysis (`temperature_dashboard/`),
- IFC model management and visualisation (`temperature_dashboard/ifc_models.py`),
- room-level analysis (`room_analysis/`: `analysis_app.py` for the view, `physics.py` for the calculations),
- SimStadt sandbox view (`simstadt_sandbox/sandbox_app.py`, read-only),
- terminal scripts (`scripts/`):
  - `run_simstadt_light.py`: block model and SimStadt runs,
  - `extract_room_envelope.py`: per-room envelope from the IFC,
  - `ingest_excel.py`, `ingest_co2.py`: data ingestion.

## License

See the repository license and included project files for the applicable terms.
