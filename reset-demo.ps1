# Resets the run history so the sample invoices tell their intended story again.
#
# WHY THIS IS NEEDED
#
# Several samples are history-dependent by design: the split-PO story needs
# 02 -> 03 -> 03b in that order, and 06 is only a duplicate because 01 ran
# first. Every run is recorded, so after a few passes 01 becomes a duplicate of
# itself and PO-1001 has no budget left -- the samples then report REJECTED and
# NEEDS_REVIEW correctly, but for reasons that have nothing to do with what
# each sample is meant to demonstrate.
#
# Clears the `runs` and `run_allocations` tables in Postgres (DATABASE_URL).
# Reference data (purchase orders, vendors, users) lives in data/*.json and is
# re-seeded on startup, so nothing that matters is lost -- only the run
# history.
#
#   .\reset-demo.ps1            reset, leaving an empty dashboard to demo into
#   .\reset-demo.ps1 -Replay    reset, then run all seven samples in order
#
param([switch]$Replay)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Reuses storage.clear_run_history() -- the exact function POST
# /api/admin/reset-demo calls -- rather than reimplementing the SQL by hand,
# so this script and the API endpoint can never disagree about what "reset"
# means. Postgres has no equivalent to SQLite's exclusive file lock, so unlike
# the old version this does not need to stop the server first: the clear runs
# in its own short-lived connection regardless of whether uvicorn is up.
& .\venv\Scripts\python.exe -c @"
import sys
sys.path.insert(0, 'backend')
import config
config.load_dotenv()
import storage
deleted = storage.clear_run_history()
print(f'Run history cleared ({deleted} run(s) removed).')
"@

if (-not $Replay) {
    Write-Host ""
    Write-Host "Done. Start the app with .\start.ps1 -- the dashboard will be empty," -ForegroundColor Cyan
    Write-Host "and the samples will behave as documented if you run them in order." -ForegroundColor Cyan
    exit 0
}

# --replay: bring the server up and drive the samples through the real API, in
# manifest order, so the dashboard is populated with the intended story.
Write-Host "Starting server..."
$server = Start-Process -PassThru -WindowStyle Hidden `
    -FilePath ".\venv\Scripts\python.exe" `
    -ArgumentList "-m", "uvicorn", "main:app", "--app-dir", "backend", "--host", "127.0.0.1", "--port", "8000"

try {
    $ready = $false
    foreach ($i in 1..30) {
        Start-Sleep -Milliseconds 500
        try {
            Invoke-RestMethod "http://127.0.0.1:8000/api/health" -TimeoutSec 2 | Out-Null
            $ready = $true
            break
        } catch { }
    }
    if (-not $ready) { throw "The server did not come up on port 8000." }

    & .\venv\Scripts\python.exe scripts\replay_samples.py
} finally {
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "Samples replayed. Start the app with .\start.ps1 to see the result." -ForegroundColor Cyan
