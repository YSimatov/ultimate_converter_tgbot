# Run from the project directory. No administrator rights are required for Python packages.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
py -3.12 -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12 first (see README.md).' }
& '.\.venv\Scripts\python.exe' -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip update failed.' }
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
& '.\.venv\Scripts\python.exe' check.py --smoke
if ($LASTEXITCODE -ne 0) { throw 'Smoke check failed.' }
Write-Host 'Ready. Set BOT_TOKEN in .env, then run .\start.ps1'
