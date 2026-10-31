# Start everything for a demo session (run from the project root in PowerShell):
#   powershell -ExecutionPolicy Bypass -File scripts\start.ps1
# Assumes the one-time setup in docs/setup-guide.md has been done.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "1/5 Docker Desktop..."
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Start-Process "$env:LOCALAPPDATA\Programs\DockerDesktop\Docker Desktop.exe"
    for ($i = 0; $i -lt 40; $i++) { Start-Sleep 5; docker info *> $null; if ($LASTEXITCODE -eq 0) { break } }
}
docker info *> $null; if ($LASTEXITCODE -ne 0) { throw "Docker is not running." }

Write-Host "2/5 MySQL..."
docker compose up -d mysql | Out-Null
for ($i = 0; $i -lt 30; $i++) {
    if ((docker inspect -f "{{.State.Health.Status}}" pds-mysql) -eq "healthy") { break }; Start-Sleep 2
}

Write-Host "3/5 Fabric containers (start any that are stopped)..."
$stopped = docker ps -a --filter "status=exited" --format "{{.Names}}" | Where-Object { $_ -match "peer0|orderer|ca_org|ca_orderer|^dev-peer" }
if ($stopped) { docker start $stopped | Out-Null; Start-Sleep 8 }

Write-Host "4/5 Fabric gateway (new window)..."
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root\fabric-gateway'; npm start"

Write-Host "5/5 Flask web app (new window)..."
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root\backend'; .\.venv\Scripts\python wsgi.py"

Start-Sleep 6
& "$root\backend\.venv\Scripts\python" "$root\scripts\health_check.py"
Write-Host "`nOpen http://localhost:5000  (log in with a seeded user; password is SEED_PASSWORD in .env)"
