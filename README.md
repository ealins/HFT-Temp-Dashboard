# Upgrade existing dashboard

1. Stop Streamlit (`Ctrl+C`).
2. Back up your current dashboard folder.
3. Copy the contents of this ZIP into the existing dashboard folder and replace matching source files.
4. **Keep your existing `.streamlit/secrets.toml` and `data/*.db` files.** This upgrade ZIP does not contain them.
5. Start again:

```bat
.venv\Scripts\activate
streamlit run app.py
```

On first start, if an existing CO₂ database is present, the packaged HFT `ID/Bau/Raum` mapping is applied once automatically to matching CO₂ sensor IDs.

Main changes: Temperature + CO₂ on one page with Normal/Technical views, compact Building → Floor → Room navigation, a separate password-protected Admin dialog for uploads and mappings, thermal comfort zone and IAQ threshold shading in charts, no daily/monthly averaging in time-series graphs, and visible line breaks across sensor gaps.

## IFC model workflow and 3D view

The Admin **IFC models** tab accepts multiple `.ifc` or `.ifczip` files. Assign each upload batch to a dashboard building, discipline/model role, and optional revision. A building may have one model or multiple federated Architecture, HVAC, Structure, Electrical, section, and other files. Administrators can enable, disable, replace-by-role, or permanently delete registered models.

IfcOpenShell extracts `IfcProject`, `IfcBuilding`, `IfcBuildingStorey`, and `IfcSpace` metadata plus triangulated `IfcSpace` geometry during ingestion. Automatic space mappings use the space LongName/Name and containing storey; the editable mapping table preserves manual Building/Floor/Room overrides. Architecture IFCs normally need to contain modeled `IfcSpace` solids for room coloring.

The **3D** dashboard mode renders active mapped spaces for Room, Floor, or Exploded building scope. It provides orbit, pan, zoom, hover details, model-role layer toggles, configurable storey separation, and Temperature/CO₂ coloring from the latest mapped room observations. Non-architecture federated files are registered and shown as layers when they contain `IfcSpace` geometry; arbitrary non-space element geometry is not currently extracted.

Persistent files are stored under `data/ifc_models/` and the registry `data/ifc_models.db`. The existing Docker Compose `./data:/app/data` volume therefore persists both source models and extracted geometry. Back it up with the paths used for reading databases. The default per-file upload limit is 500 MB; adjust Streamlit's server upload setting for very large models. If no representative project IFC is supplied with the upgrade, validate coordinate alignment, space naming, and performance with actual federated HFT models before production rollout.

## Floor-plan reference layouts (2D PDF + 3D IFC)

Administrators can upload per-floor PDF reference plans under **⚙ Admin → Floor plans**.
- Each uploaded PDF is assigned to a dashboard **Building** and **Floor**.
- Replacing a floor plan safely updates the database registry and removes the old file.
- In **3D** view mode, when a floor plan is registered for the currently selected floor, the dashboard renders the interactive 3D model on the left and the native PDF layout on the right with a download button.
- When no PDF layout is registered, the 3D viewer displays in clean full-width view with helpful guidance.
