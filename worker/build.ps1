# Build RPA-Bot.exe + RPA-Bot-Setup.exe
# Prereqs: python deps (auto-installed), Inno Setup 6 (winget install JRSoftware.InnoSetup)
# The raw dist\RPA-Bot.exe is what you upload in dashboard Settings → Publish
# Release (auto-update swaps the exe in place). RPA-Bot-Setup.exe is what you
# run on a NEW PC.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

pip install -r requirements.txt

pyinstaller --onefile --name RPA-Bot --clean --noconfirm `
  --icon assets\icon.ico `
  --add-data "assets\icon.png;assets" `
  --add-data "payloads\firewall.py;payloads" `
  worker.py

# Version for the installer from version.py
$env:RPABOT_VERSION = (Select-String -Path version.py -Pattern '"([\d.]+)"').Matches[0].Groups[1].Value

$iscc = @("$env:ProgramFiles (x86)\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
  & $iscc /Qp installer.iss
  Write-Host ""
  Write-Host "Installer:  $PSScriptRoot\dist\RPA-Bot-Setup.exe"
} else {
  Write-Host "WARNING: Inno Setup not found - installer skipped (winget install JRSoftware.InnoSetup)"
}
Write-Host "Worker exe: $PSScriptRoot\dist\RPA-Bot.exe (upload this for auto-update releases)"
