# PowerShell script to run Seagull CTF Client
param(
    [ValidateSet("session", "event")]
    [string]$Mode = "session",
    [string]$ContainerEngine = "docker"
)

$RootDir = Split-Path -Parent $PSScriptRoot
$LogsDir = Join-Path $RootDir "logs"
if (-not (Test-Path $LogsDir)) { New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null }

$ScenarioFile = if ($Mode -eq "event") { "$RootDir\scenario\ctf_event_client.xml" } else { "$RootDir\scenario\ctf_session_client.xml" }

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " Starting CTF (Charging Trigger Function) Client [$Mode] " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

if (Get-Command "seagull" -ErrorAction SilentlyContinue) {
    Write-Host "[CTF] Found native seagull binary. Launching scenario: $ScenarioFile" -ForegroundColor Green
    & seagull -conf "$RootDir\config\conf_ctf_client.xml" `
              -dico "$RootDir\config\base_rf.xml" `
              -scen "$ScenarioFile" `
              -log "$LogsDir\ctf_client.log" `
              -llevel E
} elseif (Get-Command $ContainerEngine -ErrorAction SilentlyContinue) {
    Write-Host "[CTF] Native seagull binary not found. Launching via Docker..." -ForegroundColor Yellow
    docker compose run --rm ctf /opt/seagull/run_ctf.sh $Mode
} else {
    Write-Host "[CTF] Neither Seagull nor Docker found in PATH." -ForegroundColor Red
    Write-Host "[CTF] Tip: You can run the pure Python simulation directly with:" -ForegroundColor Cyan
    Write-Host "      python python/ctf_client.py --mode $Mode" -ForegroundColor White
}
