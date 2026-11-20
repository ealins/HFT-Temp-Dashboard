# Configure a Windows Grafana install for the HFT dashboard.
# Run in an ADMIN PowerShell from the project folder:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_grafana_windows.ps1 -GrafanaHome E:\Grafana
param(
    [string]$GrafanaHome = "E:\Grafana",
    [string]$Project = (Resolve-Path "$PSScriptRoot\..").Path
)
$ErrorActionPreference = "Stop"
$bin = Join-Path $GrafanaHome "bin"
if (-not (Test-Path "$bin\grafana.exe") -and -not (Test-Path "$bin\grafana-cli.exe")) {
    throw "Grafana not found in $GrafanaHome. Install it there first, or pass -GrafanaHome <folder that contains bin\>."
}
$db   = Join-Path $Project "data\grafana\hft_sensors.db"
$dash = Join-Path $Project "grafana\dashboards"
if (-not (Test-Path $db)) { Write-Warning "$db not found yet. Run: python scripts\build_grafana_db.py" }

# 1. plugin
if (Test-Path "$bin\grafana-cli.exe") { & "$bin\grafana-cli.exe" --homepath $GrafanaHome plugins install frser-sqlite-datasource }
else { & "$bin\grafana.exe" cli --homepath $GrafanaHome plugins install frser-sqlite-datasource }

# 2. provisioning (data source + dashboard folder)
$prov = Join-Path $GrafanaHome "conf\provisioning"
New-Item -ItemType Directory -Force "$prov\datasources", "$prov\dashboards" | Out-Null
$dbYaml = $db -replace '\\', '/'
$dashYaml = $dash -replace '\\', '/'
@"
apiVersion: 1
datasources:
  - name: HFT sensors
    uid: hft-sqlite
    type: frser-sqlite-datasource
    access: proxy
    isDefault: true
    jsonData:
      path: $dbYaml
"@ | Set-Content -Encoding UTF8 "$prov\datasources\hft.yaml"
@"
apiVersion: 1
providers:
  - name: HFT
    type: file
    allowUiUpdates: true
    options:
      path: $dashYaml
"@ | Set-Content -Encoding UTF8 "$prov\dashboards\hft.yaml"

# 3. embedding + anonymous read-only viewing + default home dashboard (local use only)
$ini = Join-Path $GrafanaHome "conf\custom.ini"
$homeDash = ($dash -replace '\\', '/') + "/hft-rooms.json"
@"
[security]
allow_embedding = true

[auth.anonymous]
enabled = true
org_role = Viewer

[dashboards]
default_home_dashboard_path = $homeDash
"@ | Set-Content -Encoding UTF8 $ini

# 4. restart
$svc = Get-Service | Where-Object { $_.Name -like "*grafana*" } | Select-Object -First 1
if ($svc) { Restart-Service $svc.Name; Write-Host "Restarted service $($svc.Name)." }
else { Write-Host "No Grafana service found. Start it with: $bin\grafana-server.exe --homepath $GrafanaHome" }
Write-Host "Done. Open http://localhost:3000"
