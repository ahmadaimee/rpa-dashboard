"""Self-update: check worker_releases for a newer version, download from the
private worker-releases bucket, verify sha256, swap the exe and restart.

The swap uses a helper .bat that waits for this process to exit, copies the
new exe over the old one, and restarts via the Task Scheduler entry (falls
back to launching the exe directly).
"""
import hashlib
import logging
import os
import subprocess
import sys

from . import config as cfgmod
from .cloud import Cloud
from .installer import TASK_NAME

log = logging.getLogger("worker")


def _ver_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.strip().lstrip("v").split("."))
    except Exception:
        return (0,)


def check_and_apply(cloud: Cloud, current_version: str) -> bool:
    """Returns True if an update was launched (process will exit)."""
    rel = cloud.latest_release()
    if not rel:
        return False
    latest = rel.get("version", "")
    if _ver_tuple(latest) <= _ver_tuple(current_version):
        return False

    log.info("Update available: %s → %s", current_version, latest)

    if not cfgmod.is_frozen():
        log.info("Dev mode (not frozen) — skipping self-update")
        return False

    data = cloud.download_release(rel["storage_path"])
    if not data:
        log.error("Update download failed: %s", rel["storage_path"])
        return False

    digest = hashlib.sha256(data).hexdigest()
    if digest.lower() != (rel.get("sha256") or "").lower():
        log.error("Update sha256 mismatch (got %s, expected %s) — aborting",
                  digest[:12], (rel.get("sha256") or "")[:12])
        return False

    target = cfgmod.exe_path()
    update_dir = cfgmod.CONFIG_DIR / "update"
    update_dir.mkdir(parents=True, exist_ok=True)
    new_exe = update_dir / "OrchardRPAWorker.new.exe"
    new_exe.write_bytes(data)

    # Full System32 paths: immune to PATH oddities; ping as sleep (timeout.exe
    # fails without a console). NOTE: DETACHED_PROCESS must NOT be combined
    # with CREATE_NO_WINDOW — that combo silently prevents cmd from running
    # the batch at all (caused the v1.2.0 update hang).
    sys32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    bat = update_dir / "apply_update.bat"
    bat.write_text(f"""@echo off
:waitloop
"{sys32}\\tasklist.exe" /FI "PID eq {os.getpid()}" /NH | "{sys32}\\findstr.exe" /C:"{os.getpid()}" >nul
if not errorlevel 1 (
  "{sys32}\\ping.exe" -n 2 127.0.0.1 >nul
  goto waitloop
)
copy /y "{new_exe}" "{target}" >nul
del "{new_exe}" >nul 2>&1
"{sys32}\\schtasks.exe" /run /tn "{TASK_NAME}" >nul 2>&1 || start "" "{target}" --background
(goto) 2>nul & del "%~f0"
""", encoding="ascii")

    log.info("Applying update %s — restarting", latest)
    cloud.set_status("offline")
    subprocess.Popen(["cmd", "/c", str(bat)], creationflags=(
        subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP))
    os._exit(0)
