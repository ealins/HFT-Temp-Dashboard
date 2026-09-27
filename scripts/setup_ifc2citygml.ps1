# HFT Temperature & CO2 Dashboard
# Install the TUM-GIS IFC -> CityGML 3.0 converter locally.
# This keeps conversion outside the dashboard virtual environment.

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$ToolsRoot = Join-Path $Root ".tools"
$ConverterRoot = Join-Path $ToolsRoot "ifc-to-citygml3"
$VenvRoot = Join-Path $ConverterRoot ".venv"
$PythonExe = Join-Path $VenvRoot "Scripts\python.exe"
$RepoUrl = "https://github.com/tum-gis/ifc-to-citygml3.git"

New-Item -ItemType Directory -Force -Path $ToolsRoot | Out-Null

if (-not (Test-Path (Join-Path $ConverterRoot "ifc2citygml.py"))) {
    Write-Host "Cloning TUM-GIS IFC-to-CityGML 3.0 converter..."
    git clone --depth 1 $RepoUrl $ConverterRoot
} else {
    Write-Host "Updating local TUM-GIS converter..."
    git -C $ConverterRoot pull --ff-only
}

if (-not (Test-Path $PythonExe)) {
    Write-Host "Creating dedicated converter Python 3.12 environment..."
    py -3.12 -m venv $VenvRoot
}

Write-Host "Installing converter dependencies..."
& $PythonExe -m pip install --upgrade pip
& $PythonExe -m pip install -r (Join-Path $ConverterRoot "requirements.txt")

Write-Host ""
Write-Host "Local IFC-to-CityGML converter is ready."
Write-Host "Converter: $(Join-Path $ConverterRoot 'ifc2citygml.py')"
Write-Host "Python:    $PythonExe"
Write-Host ""
Write-Host "Start the dashboard normally with:"
Write-Host "  streamlit run app.py"
