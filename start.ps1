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
# If Node is unavailable the app still starts: backend/main.py falls back to the
# original vanilla frontend, so a machine without npm is not locked out.
if (-not (Test-Path ".\frontend-next\out\index.html")) {
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        Write-Host "Building the UI (first run only, takes about a minute)..."
        Push-Location ".\frontend-next"
        if (-not (Test-Path ".\node_modules")) { npm install --no-audit --no-fund }
        npm run build
        Pop-Location
    } else {
        Write-Host "npm not found - serving the fallback UI." -ForegroundColor Yellow
    }
}

Write-Host "Starting server at http://127.0.0.1:8000 ..."
Start-Process "http://127.0.0.1:8000"
& .\venv\Scripts\python.exe -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000
