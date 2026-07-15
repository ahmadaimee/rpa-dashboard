# Build OrchardRPAWorker.exe
# Usage:  .\build.ps1                       (console build — recommended while testing)
#         .\build.ps1 -NoConsole            (hidden build for rollout)
# Fill worker/embedded.py (or set ORCHARD_SUPABASE_URL / ORCHARD_SUPABASE_ANON_KEY
# env vars at *runtime* of the exe won't work — they must be present in embedded.py
# or in the environment at BUILD time is NOT enough; edit embedded.py directly).
param([switch]$NoConsole)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

pip install -r requirements.txt

$flags = @("--onefile", "--name", "OrchardRPAWorker", "--clean", "--noconfirm")
if ($NoConsole) { $flags += "--noconsole" }

pyinstaller @flags worker.py

Write-Host ""
Write-Host "Build complete: $PSScriptRoot\dist\OrchardRPAWorker.exe"
