# Install SimStadt locally for the HFT dashboard.
# Run from the repository root in PowerShell.
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$SimstadtExe = Join-Path $RepoRoot ".venv\Scripts\simstadt.exe"

if (-not (Test-Path $Python)) {
    throw "Dashboard Python environment not found at $Python. Create/activate .venv first."
}

Write-Host "Installing the current SimStadt Python CLI into the dashboard environment..."
& $Python -m pip install --upgrade simstadt

if (Test-Path $SimstadtExe) {
    Write-Host "Downloading/installing the current SimStadt runtime..."
    & $SimstadtExe --install

    Write-Host ""
    Write-Host "Checking SimStadt CLI..."
    & $SimstadtExe --version
} else {
    Write-Warning "The simstadt console script was not found after installation."
    Write-Host "Try: $Python -m simstadt --install"
}

$java = Get-Command java -ErrorAction SilentlyContinue
if (-not $java) {
    Write-Warning "Java was not found on PATH. Current SimStadt documentation requires Java 17+ for local execution."
    Write-Warning "Install a Java 17+ distribution (Liberica Full JRE/JDK is documented as a compatible option) and restart VS Code."
}

Write-Host ""
Write-Host "SimStadt local setup completed."
Write-Host "Restart Streamlit, then open the SimStadt Sandbox."
