# HFT Temperature & CO₂ Dashboard

The **HFT Temperature & CO₂ Dashboard** is a comprehensive web application built with Streamlit for monitoring, analyzing, and visualizing indoor climate data (Temperature and CO₂ levels) across different buildings, floors, and rooms. 

The application provides an interactive interface for facility managers, administrators, and general users to gain insights into building performance, thermal comfort, and indoor air quality (IAQ).

## 🚀 Key Functions & Features

### 1. Data Visualization & Analytics
- **Unified Dashboard**: View both Temperature and CO₂ metrics on a single page, with Normal and Technical viewing modes.
- **Hierarchical Navigation**: Compact and intuitive navigation filtering by Building → Floor → Room.
- **Time-Series Charts**: Interactive timelines for temperature and CO₂ with thermal comfort zone and IAQ threshold shading. Gaps in sensor data are represented with visible line breaks.
- **Carpet Plots (Heatmaps)**: Dense visual overview of historical data to quickly identify patterns over days and hours.
- **Distribution & Gap Stats**: Statistical overview and data completeness analysis.

### 2. 3D IFC Model Viewer
- **Interactive 3D Context**: Renders mapped spaces from uploaded IFC (Industry Foundation Classes) BIM models.
- **Climate Coloring**: Automatically colors 3D rooms based on the latest mapped Temperature/CO₂ observations.
- **Viewer Controls**: Support for Room, Floor, or Exploded building scopes. Includes orbit, pan, zoom, hover details, and configurable storey separation.
- **Layer Management**: Toggle model-role layers (Architecture, HVAC, Structure, Electrical, etc.).

### 3. 2D Floor Plan References
- **PDF Layouts**: Upload and view per-floor 2D PDF reference plans.
- **Side-by-Side View**: When viewing a floor in 3D mode, the 3D model is shown interactively alongside the native PDF layout.

### 4. Admin & Data Management
- **Password-Protected Admin Panel**: Secure dialog for uploads, mappings, and configuration.
- **Data Ingestion**: Directly upload and ingest Excel files for Temperature and CSV/files for CO₂ data.
- **Sensor Mapping**: Map raw sensor IDs to physical `Building/Floor/Room` locations.
- **IFC Model Management**: Upload `.ifc` or `.ifczip` files, assign them to buildings and disciplines/roles, and manage space-to-sensor mappings.
- **Floor Plan Management**: Assign reference PDF floor plans to specific buildings and floors.

## 📖 User Guide: How to Use the Dashboard

### 1. Navigating to a Room
Use the sidebar on the left to drill down to the specific location you want to analyze:
1. Select a **Building**.
2. Select a **Floor** within that building.
3. Select a specific **Room**.
*Tip: The dashboard will automatically update all charts and 3D views to reflect the selected location.*

### 2. Normal vs. Technical View
At the top of the dashboard, you can toggle between two viewing modes:
- **Normal View**: A streamlined, easy-to-read interface focusing on key comfort indicators (is it too hot/cold? is the air quality good?). Ideal for quick checks.
- **Technical View**: Displays detailed time-series charts, carpet plots (heatmaps), and statistical distributions for in-depth data analysis by facility managers.

### 3. Understanding the Charts (Technical View)
- **Time-Series**: Shows Temperature and CO₂ levels over time. Colored background bands indicate optimal thermal comfort zones and healthy Indoor Air Quality (IAQ) thresholds. 
- **Carpet Plots**: A grid where days are on one axis and hours on the other. Darker/lighter colors help spot recurring daily patterns (e.g., heating turning on at 6 AM).

### 4. Exploring the 3D Model & Floor Plans
- Switch to the **3D** tab to see a 3D representation of the selected space.
- Rooms are color-coded based on their latest Temperature or CO₂ readings.
- Use your mouse to rotate, pan, and zoom the 3D model. 
- If a 2D PDF floor plan is available, it will be displayed side-by-side with the 3D model for easy reference.

---

## 🛠 Upgrade & Installation Instructions

1. Stop Streamlit (`Ctrl+C`).
2. Back up your current dashboard folder.
3. Copy the contents of this ZIP into the existing dashboard folder and replace matching source files.
4. **Keep your existing `.streamlit/secrets.toml` and `data/*.db` files.** This upgrade ZIP does not contain them.
5. Start again:

```bat
.venv\Scripts\activate
streamlit run app.py
```

*Note: On first start, if an existing CO₂ database is present, the packaged HFT `ID/Bau/Raum` mapping is applied once automatically to matching CO₂ sensor IDs.*

## 📁 Technical Architecture & Workflow

### Git Repository Information
This repository is initialized with Git and currently tracks changes on the `main` branch. 

### IFC Model Workflow
IfcOpenShell extracts `IfcProject`, `IfcBuilding`, `IfcBuildingStorey`, and `IfcSpace` metadata plus triangulated `IfcSpace` geometry during ingestion. Automatic space mappings use the space LongName/Name and containing storey; the editable mapping table preserves manual Building/Floor/Room overrides. Architecture IFCs normally need to contain modeled `IfcSpace` solids for room coloring.

Non-architecture federated files are registered and shown as layers when they contain `IfcSpace` geometry; arbitrary non-space element geometry is not currently extracted.

Persistent files are stored under `data/ifc_models/` and the registry `data/ifc_models.db`. The existing Docker Compose `./data:/app/data` volume therefore persists both source models and extracted geometry. Back it up with the paths used for reading databases. The default per-file upload limit is 500 MB; adjust Streamlit's server upload setting for very large models. If no representative project IFC is supplied with the upgrade, validate coordinate alignment, space naming, and performance with actual federated HFT models before production rollout.
