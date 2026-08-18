# Launches the Invoice Processing app: installs deps (first run only),
# regenerates sample invoices if missing, starts the API + UI, opens the browser.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".\venv")) {
    Write-Host "Creating virtual environment..."
    python -m venv venv
}

Write-Host "Installing dependencies..."
& .\venv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .\venv\Scripts\python.exe -m pip install --quiet -r requirements.txt

if (-not (Test-Path ".\sample_invoices\01_happy_path_acme.pdf")) {
    Write-Host "Generating sample invoices..."
    & .\venv\Scripts\python.exe sample_invoices\generate_invoices.py
}

Write-Host "Starting server at http://127.0.0.1:8000 ..."
Start-Process "http://127.0.0.1:8000"
& .\venv\Scripts\python.exe -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000
