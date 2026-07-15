# Build OrchardRPAWorker.exe (console build — required so the first-run
# pairing prompt works; --background mode hides its own console window).
# Prereq: worker/embedded.py must contain the real Supabase URL + anon key.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

pip install -r requirements.txt

pyinstaller --onefile --name OrchardRPAWorker --clean --noconfirm worker.py

Write-Host ""
Write-Host "Build complete: $PSScriptRoot\dist\OrchardRPAWorker.exe"
