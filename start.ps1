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

# Build the Next.js UI if it has never been built. The output is a STATIC
# export -- no Node process runs at serve time, uvicorn hands out plain files.
# This is the only UI now (the vanilla frontend/ fallback was removed), so npm
# is required on first run.
if (-not (Test-Path ".\frontend-next\out\index.html")) {
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        Write-Host "Building the UI (first run only, takes about a minute)..."
        Push-Location ".\frontend-next"
        if (-not (Test-Path ".\node_modules")) { npm install --no-audit --no-fund }
        npm run build
        Pop-Location
    } else {
        Write-Host "npm not found. Install Node.js, then run 'npm run build' inside frontend-next\ before starting the server." -ForegroundColor Red
        exit 1
    }
}

Write-Host "Starting server at http://127.0.0.1:8000 ..."
Start-Process "http://127.0.0.1:8000"
& .\venv\Scripts\python.exe -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000
