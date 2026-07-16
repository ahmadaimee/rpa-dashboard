"""First-run setup: pairing-code registration, Task Scheduler autostart,
background relaunch, single-instance guard, uninstall."""
import json
import logging
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config as cfgmod
from .config import CONFIG_DIR, PID_FILE, USERNAME

log = logging.getLogger("worker")

CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0

TASK_NAME = f"RPA-Bot-Worker-{USERNAME}"


# ── register with the cloud via the edge function ────────────
def register(supabase_url: str, anon_key: str, code: str,
             display_name: str | None, app_version: str) -> dict:
    url = f"{supabase_url.rstrip('/')}/functions/v1/register-device"
    body = json.dumps({
        "code": code,
        "username": USERNAME,
        "hostname": socket.gethostname(),
        "display_name": display_name or USERNAME,
        "app_version": app_version,
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "apikey": anon_key,
        "Authorization": f"Bearer {anon_key}",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("error", "")
        except Exception:
            detail = ""
        raise RuntimeError(f"Registration failed ({e.code}): {detail or e.reason}")


def interactive_install(supabase_url: str, anon_key: str, app_version: str):
    print()
    print("  +----------------------------------------------+")
    print("  |     RPA Agent — Worker Setup                  |")
    print("  +----------------------------------------------+")
    print(f"  PC / User : {USERNAME} @ {socket.gethostname()}")
    print()

    if "YOUR-PROJECT" in supabase_url or "YOUR-ANON-KEY" in anon_key:
        print("  ❌ This build has no Supabase URL/key baked in.")
        print("     Rebuild with build.ps1 after filling worker/embedded.py")
        input("  Press Enter to close...")
        sys.exit(1)

    code = input("  Pairing code (from dashboard Settings tab): ").strip()
    if not code:
        print("  No code entered — aborting.")
        sys.exit(1)
    display = input(f"  Display name for this PC [{USERNAME}]: ").strip() or None

    print("  Registering with the cloud...")
    creds = register(supabase_url, anon_key, code, display, app_version)

    cfgmod.save({
        "supabase_url": supabase_url,
        "anon_key": anon_key,
        "email": creds["email"],
        "password": creds["password"],
        "worker_id": creds["worker_id"],
        "username": USERNAME,
        "display_name": display or USERNAME,
    })
    print(f"  ✅ Registered (worker id {creds['worker_id'][:8]}…)")

    print("  Installing startup task so the worker survives reboots...")
    install_startup_task()
    print("  Launching worker in the background...")
    relaunch_background()

    started = False
    for _ in range(10):
        time.sleep(1)
        if PID_FILE.exists():
            started = True
            break
    print()
    if started:
        print("  ✅ Done! The worker is running silently in the background.")
        print("     This PC will appear online in the dashboard within seconds.")
    else:
        print("  ⚠  Worker may not have started — check the log:")
        print(f"     {cfgmod.LOG_FILE}")
    print()
    time.sleep(4)


# ── Task Scheduler (port of install_startup_task) ────────────
def _exec_command_and_args() -> tuple[str, str]:
    if cfgmod.is_frozen():
        return sys.executable, "--background"
    pyw = Path(sys.executable).parent / "pythonw.exe"
    py = str(pyw) if pyw.exists() else sys.executable
    return py, f'"{cfgmod.exe_path()}" --background'


def install_startup_task():
    if platform.system() != "Windows":
        log.warning("Startup task: Windows only — skipped")
        return
    command, arguments = _exec_command_and_args()
    workdir = str(Path(cfgmod.exe_path()).parent)
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>RPA Agent — {USERNAME}</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled>
      <UserId>{os.environ.get("USERDOMAIN", ".")}\\{USERNAME}</UserId>
    </LogonTrigger>
    <!-- Watchdog: fires every 5 min; IgnoreNew means while the worker is
         alive the running task instance makes these ticks no-ops, but if
         the worker ever dies it is restarted within 5 minutes. -->
    <CalendarTrigger>
      <StartBoundary>2020-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
      <Repetition>
        <Interval>PT5M</Interval>
        <Duration>P1D</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Hidden>true</Hidden>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    xml_path = CONFIG_DIR / "_task.xml"
    xml_path.write_text(xml, encoding="utf-16")
    try:
        r = subprocess.run(
            ["schtasks", "/create", "/tn", TASK_NAME, "/xml", str(xml_path), "/f"],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode == 0:
            log.info("Startup task '%s' installed", TASK_NAME)
        else:
            log.error("schtasks error: %s", r.stderr.strip())
    finally:
        xml_path.unlink(missing_ok=True)


def remove_startup_task():
    if platform.system() != "Windows":
        return
    r = subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
                       capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    if r.returncode == 0:
        log.info("Startup task '%s' removed", TASK_NAME)
    else:
        log.warning("Could not remove task (may not exist): %s", r.stderr.strip())


def relaunch_background():
    """Prefer schtasks /run — guarantees the same hidden config as boot."""
    if platform.system() == "Windows":
        r = subprocess.run(["schtasks", "/run", "/tn", TASK_NAME],
                           capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        if r.returncode == 0:
            return
        log.warning("schtasks /run failed (%s) — direct spawn", r.stderr.strip())
    if cfgmod.is_frozen():
        cmd = [sys.executable, "--background"]
    else:
        py = _exec_command_and_args()[0]
        cmd = [py, cfgmod.exe_path(), "--background"]
    flags = 0
    if platform.system() == "Windows":
        # DETACHED_PROCESS + CREATE_NO_WINDOW are mutually exclusive — combo
        # silently breaks the child. Use NO_WINDOW only.
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(cmd, creationflags=flags)


# ── single-instance guard ────────────────────────────────────
def _pid_alive(pid: int) -> bool:
    """Alive AND actually one of ours — Windows reuses PIDs, and a stale PID
    file matching some unrelated process caused false 'already running'."""
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=5,
                             creationflags=CREATE_NO_WINDOW).stdout
        if str(pid) not in out:
            return False
        image = out.strip().split()[0].lower() if out.strip() else ""
        return "rpa-bot" in image or "python" in image
    except Exception:
        return False


def is_already_running() -> bool:
    if PID_FILE.exists():
        try:
            return _pid_alive(int(PID_FILE.read_text().strip()))
        except Exception:
            pass
    return False


def kill_running_worker() -> bool:
    """Kill the background worker recorded in the PID file (upgrade path)."""
    killed = False
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            if pid != os.getpid() and _pid_alive(pid):
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, creationflags=CREATE_NO_WINDOW)
                killed = True
        except Exception:
            pass
        PID_FILE.unlink(missing_ok=True)
    return killed


def acquire_instance_lock():
    import atexit
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    atexit.register(lambda: PID_FILE.unlink(missing_ok=True))


# ── uninstall ────────────────────────────────────────────────
def uninstall(cloud=None):
    print("\n  RPA Agent — Uninstaller")
    print("  =====================")
    remove_startup_task()
    # Kill a running background worker
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            if pid != os.getpid() and _pid_alive(pid):
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                               creationflags=CREATE_NO_WINDOW)
                print(f"  Killed running worker (PID {pid})")
        except Exception:
            pass
        PID_FILE.unlink(missing_ok=True)
    if cloud:
        try:
            cloud.set_status("offline")
        except Exception:
            pass
    # Clear the registration so a fresh install re-pairs cleanly
    if cfgmod.CONFIG_FILE.exists():
        cfgmod.CONFIG_FILE.unlink(missing_ok=True)
        print("  Registration cleared (config.json deleted).")
    print("  ✅ Worker removed from this PC.")
    print("     Remove/disable the PC in the dashboard Settings tab to revoke access.")


def clear_registration():
    """Wipe local credentials so the next run pairs fresh."""
    cfgmod.CONFIG_FILE.unlink(missing_ok=True)
