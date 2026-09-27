# HFT Temperature & CO₂ Dashboard

A web-based dashboard for monitoring, exploring, and analysing indoor **Temperature** and **CO₂** conditions across buildings, floors, and rooms.

The dashboard combines sensor data with **IFC building models**, interactive 3D room context, floor plans, and optional **SimStadt** energy and environmental analysis.

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

## SimStadt Analysis

The dashboard can be used together with **SimStadt** for building energy and environmental analysis.

Available workflows:

| Workflow | Description |
|---|---|
| **Heat Demand** | Analyse annual building heating demand |
| **Hourly Heat Demand** | Analyse time-dependent heating demand |
| **Environmental / CO₂ Analysis** | Analyse environmental and CO₂-emission results |

### IFC to CityGML

When an appropriate Architecture IFC is available, the dashboard can prepare a **CityGML 3.0** model for SimStadt automatically.

An existing CityGML model can also be supplied directly.

### Sensor Data and Simulation

Temperature and indoor CO₂ observations can provide useful measured building-condition information for interpreting simulation results.

The dashboard can summarise:

- observed mean and percentile Temperature,
- observed indoor CO₂ levels,
- an occupancy indicator derived from CO₂,
- a diagnostic CO₂-decay ventilation indicator.

These measurements can be used alongside simulation results for comparison and interpretation.

> **Important:** indoor CO₂ concentration is measured in **ppm**, while environmental CO₂ results from energy analysis represent emissions. They are different physical quantities and should not be compared as the same variable.

## Getting Started

### Requirements

For the basic dashboard:

- Python 3.12 recommended
- A modern web browser

For IFC functionality:

- IFC/IFCZIP building models
- IFC files containing usable `IfcSpace` information for room-level visualisation

For SimStadt analysis:

- A working SimStadt installation or compatible execution environment
- CityGML input, or an IFC model that can be converted to CityGML

### Recommended IFC → CityGML setup

For Windows users, the recommended route is the **direct local TUM-GIS converter**. It avoids Docker startup and file-mount overhead and lets the dashboard reuse a cached CityGML model when the source IFC has not changed.

Run once from the repository root:

~~~text
powershell -ExecutionPolicy Bypass -File .\\scripts\\setup_ifc2citygml.ps1
~~~

The setup creates a dedicated converter environment under the local `.tools/` directory. The dashboard then detects it automatically.

Docker remains available as a fallback when the local converter is not installed.

The dashboard caches each IFC → CityGML conversion using the source file timestamp/size and converter version. Re-running an unchanged model therefore does not repeat the expensive geometry conversion.

### SimStadt local runtime

The current SimStadt CLI can be installed from PyPI and can download/install the current SimStadt runtime locally.

Run once:

~~~text
powershell -ExecutionPolicy Bypass -File .\\scripts\\setup_simstadt.ps1
~~~

The dashboard automatically looks for the local `simstadt` command or Python module before falling back to Docker.

The current Docker fallback image is `simstadt/simstadt:cli`.

For local SimStadt execution, the current SimStadt documentation requires Java 17+ and also lists INSEL as a required component for the full application/workflow environment.

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

### 5. Run SimStadt Analysis

From the SimStadt analysis view:

1. Select the building and location of interest.
2. Provide an existing CityGML model or use the available IFC model as the source.
3. Select the required workflow:
   - Heat Demand
   - Hourly Heat Demand
   - Environmental / CO₂ Analysis
4. Start the analysis.
5. Review the generated simulation results.
6. Compare the results with the available measured building conditions.

Long analyses can continue while the dashboard remains usable, and the results can be inspected from the dashboard when processing is complete.

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
- SimStadt heating-demand results,
- hourly heating-demand results,
- environmental/CO₂-emission results,
- downloadable analysis tables and simulation outputs.

## Data and Privacy

The dashboard is intended to operate on building and sensor datasets supplied by the deployment owner.

When using real building or occupancy-related data, ensure that the dataset and deployment comply with the applicable institutional, contractual, and data-protection requirements.

## Technical Resources

- **TUM-GIS IFC → CityGML 3.0:** https://github.com/tum-gis/ifc-to-citygml3
- **SimStadt:** https://simstadt.hft-stuttgart.de/

## Project Structure

The main application is started with:

~~~text
app.py
~~~

Core functionality is organised into modules for:

- dashboard and sensor analysis,
- IFC model management and visualisation,
- data ingestion and storage,
- SimStadt analysis.

## License

See the repository license and included project files for the applicable terms.
