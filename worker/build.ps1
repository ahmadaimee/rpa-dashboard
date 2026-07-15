# Build RPA-Bot.exe (console build — required so the first-run pairing
# prompt works; --background mode hides its own console and shows a tray icon).
# Prereq: worker/embedded.py must contain the real Supabase URL + anon key.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

pip install -r requirements.txt

pyinstaller --onefile --name RPA-Bot --clean --noconfirm `
  --icon assets\icon.ico `
  --add-data "assets\icon.png;assets" `
  worker.py

Write-Host ""
Write-Host "Build complete: $PSScriptRoot\dist\RPA-Bot.exe"
