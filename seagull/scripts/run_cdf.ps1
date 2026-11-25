# PowerShell script to run Seagull CDF Server (via Docker if native binary is unavailable)
param(
    [string]$ContainerEngine = "docker"
)

$RootDir = Split-Path -Parent $PSScriptRoot
$LogsDir = Join-Path $RootDir "logs"
if (-not (Test-Path $LogsDir)) { New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null }

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " Starting CDF (Charging Data Function) Server Simulation " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

if (Get-Command "seagull" -ErrorAction SilentlyContinue) {
    Write-Host "[CDF] Found native seagull binary. Launching..." -ForegroundColor Green
    & seagull -conf "$RootDir\config\conf_cdf_server.xml" `
              -dico "$RootDir\config\base_rf.xml" `
              -scen "$RootDir\scenario\cdf_server.xml" `
              -log "$LogsDir\cdf_server.log" `
              -llevel E
} elseif (Get-Command $ContainerEngine -ErrorAction SilentlyContinue) {
    Write-Host "[CDF] Native seagull binary not found. Launching via Docker..." -ForegroundColor Yellow
    docker compose up cdf
} else {
    Write-Host "[CDF] Neither Seagull nor Docker found in PATH." -ForegroundColor Red
    Write-Host "[CDF] Tip: You can run the pure Python simulation directly with:" -ForegroundColor Cyan
    Write-Host "      python python/cdf_server.py --port 3868" -ForegroundColor White
}
