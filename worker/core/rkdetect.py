"""Detect real Keyence RK-10 activity — including runs a user starts directly
from the Keyence UI on the worker PC — and support stopping them.

Signals (Keyence reuses a per-scenario GUID log folder and only writes
RunningLog.json at run END, so file-presence alone is not enough):

1. CPU activity of KEYENCE dotnet runner processes
   (scenarios execute as `dotnet ...\\KEYENCE\\RkScenarioManager\\BuildCache\\
   <ver>\\<ScenarioHash>\\Temp\\...Temp.dll`). We track per-PID cpu-time
   deltas between heartbeats; an active delta = scenario executing.
2. A run-log GUID folder with no RunningLog.json yet (first run of a
   scenario) = run in progress.
3. A FRESH RunningLog.json write = the run just ended (and names it).

State machine with hysteresis: once running, we stay 'running' until a
fresh end-write appears or everything stays quiet for several samples.
"""
import logging
import os
import platform
import re
import subprocess
import time
from pathlib import Path

log = logging.getLogger("worker")

RK_LOG_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "KEYENCE" / "RkScenarioManager" / "Log"
FRESH_FOLDER_SECS = 120             # folder-without-log only *starts* a run if this fresh
CPU_ACTIVE_100NS = 1_500_000        # 0.15 s cpu-time between samples (~1.5%/10s)
QUIET_SAMPLES_TO_END = 6            # ~60 s of silence → consider the run over

CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0

_SCEN_RE = re.compile(r'"ScenarioPath"\s*:\s*"((?:[^"\\]|\\.)*)"')
_HASH_RE = re.compile(r'"ScenarioHash"\s*:\s*"([0-9a-fA-F]+)"')
_BUILD_HASH_RE = re.compile(r"BuildCache\\[^\\]+\\([0-9a-fA-F]{32,})\\")


def rk_app_open() -> bool:
    if platform.system() != "Windows":
        return False
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq RkScenarioManager.exe", "/NH"],
            text=True, creationflags=CREATE_NO_WINDOW,
        )
        return "RkScenarioManager.exe" in out
    except Exception:
        return False


def _scenario_name(path_escaped: str) -> str:
    return Path(path_escaped.replace("\\\\", "\\")).stem


def keyence_dotnet_procs() -> list[dict]:
    """[{pid, cputime(100ns), hash|None}] for KEYENCE scenario runner processes."""
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='dotnet.exe'\" | "
             "Where-Object { $_.CommandLine -like '*KEYENCE*RkScenarioManager*' } | "
             "ForEach-Object { '{0}|{1}|{2}' -f $_.ProcessId, "
             "($_.UserModeTime + $_.KernelModeTime), $_.CommandLine }"],
            text=True, timeout=30, creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        log.debug("keyence_dotnet_procs error: %s", e)
        return []
    procs = []
    for line in out.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        h = _BUILD_HASH_RE.search(parts[2])
        procs.append({"pid": int(parts[0]), "cputime": int(parts[1] or 0),
                      "hash": h.group(1).lower() if h else None})
    return procs


def kill_keyence_dotnets(pids: list[int] | None = None):
    """Force-kill the scenario runner processes (used for remote Stop)."""
    targets = pids or [p["pid"] for p in keyence_dotnet_procs()]
    for pid in targets:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    if targets:
        log.info("Killed %d Keyence runner process(es)", len(targets))


def log_baseline() -> dict:
    """Snapshot of RunningLog mtimes — take before starting a run."""
    base = {}
    if RK_LOG_DIR.is_dir():
        try:
            for f in RK_LOG_DIR.glob("*/RunningLog.json"):
                base[str(f)] = f.stat().st_mtime
        except Exception:
            pass
    return base


def wait_run_end(stop_event, baseline: dict, poll_secs: float = 3.0,
                 quiet_needed: int = 7):
    """Block until the Keyence run appears finished: a fresh RunningLog write
    (vs baseline) OR the KEYENCE dotnet runners staying cpu-quiet for
    ~quiet_needed*poll_secs. Replaces the old 'wait until RkScenarioManager.exe
    disappears' logic, which never ended when the Keyence app itself was open.
    """
    last_cpu: dict[int, int] = {}
    quiet = 0
    while not stop_event.is_set():
        # 1) fresh end-write?
        if RK_LOG_DIR.is_dir():
            try:
                for f in RK_LOG_DIR.glob("*/RunningLog.json"):
                    key = str(f)
                    if f.stat().st_mtime > baseline.get(key, 0):
                        return
            except Exception:
                pass
        # 2) runner processes cpu-quiet?
        procs = keyence_dotnet_procs()
        if not procs:
            return
        active = any(
            last_cpu.get(p["pid"]) is not None
            and p["cputime"] - last_cpu[p["pid"]] > int(CPU_ACTIVE_100NS * poll_secs / 10)
            for p in procs
        )
        last_cpu = {p["pid"]: p["cputime"] for p in procs}
        quiet = 0 if active else quiet + 1
        if quiet >= quiet_needed:
            return
        time.sleep(poll_secs)


class RkMonitor:
    """Stateful detector — call sample() from the heartbeat loop (~10 s)."""

    def force_end(self):
        """The run was killed by us — report idle immediately."""
        self._running = False
        self._quiet = 0
        self._scenario = None
        self._active_pids = []

    def __init__(self):
        self._last_cpu: dict[int, int] = {}
        self._known_logs: dict[str, float] = {}   # RunningLog path → mtime
        self._hash_names: dict[str, str] = {}
        self._hash_names_at = 0.0
        self._running = False
        self._quiet = 0
        self._scenario: str | None = None
        self._active_pids: list[int] = []
        self._primed = False

    # ── helpers ─────────────────────────────────────────────
    def _refresh_hash_names(self, force=False):
        if not force and time.time() - self._hash_names_at < 300:
            return
        self._hash_names_at = time.time()
        if not RK_LOG_DIR.is_dir():
            return
        try:
            files = sorted(RK_LOG_DIR.glob("*/RunningLog.json"),
                           key=lambda f: f.stat().st_mtime, reverse=True)[:200]
            for f in files:
                text = f.read_text(encoding="utf-8", errors="ignore")
                h, s = _HASH_RE.search(text), _SCEN_RE.search(text)
                if h and s:
                    self._hash_names.setdefault(h.group(1).lower(), _scenario_name(s.group(1)))
        except Exception as e:
            log.debug("hash map refresh error: %s", e)

    def _scan_logs(self) -> tuple[bool, str | None]:
        """Returns (folder_without_log, just_ended_name)."""
        no_log = False
        ended_name = None
        if not RK_LOG_DIR.is_dir():
            return no_log, ended_name
        now = time.time()
        try:
            for d in RK_LOG_DIR.iterdir():
                if not d.is_dir() or d.name.lower() == "license":
                    continue
                f = d / "RunningLog.json"
                if not f.exists():
                    # Start-trigger only when the folder is brand new — an
                    # aborted/killed run leaves a log-less folder behind, and
                    # that must not keep signalling "running" for hours.
                    if now - d.stat().st_mtime < FRESH_FOLDER_SECS:
                        no_log = True
                    continue
                mtime = f.stat().st_mtime
                key = str(f)
                prev = self._known_logs.get(key)
                self._known_logs[key] = mtime
                # fresh write since the previous sample = a run just ended
                if self._primed and prev is not None and mtime > prev:
                    try:
                        s = _SCEN_RE.search(f.read_text(encoding="utf-8", errors="ignore"))
                        ended_name = _scenario_name(s.group(1)) if s else ended_name
                    except Exception:
                        pass
                elif self._primed and prev is None:
                    # brand-new log file (first run of a scenario) also = ended
                    try:
                        s = _SCEN_RE.search(f.read_text(encoding="utf-8", errors="ignore"))
                        ended_name = _scenario_name(s.group(1)) if s else ended_name
                    except Exception:
                        pass
        except Exception as e:
            log.debug("log scan error: %s", e)
        return no_log, ended_name

    # ── main entry ──────────────────────────────────────────
    def sample(self) -> dict:
        """{"open", "running", "scenario", "event": None|'started'|'ended',
            "ended_name", "active_pids"}"""
        opened = rk_app_open()
        procs = keyence_dotnet_procs() if opened else []

        active_pids = []
        for p in procs:
            prev = self._last_cpu.get(p["pid"])
            if prev is not None and p["cputime"] - prev > CPU_ACTIVE_100NS:
                active_pids.append(p["pid"])
        self._last_cpu = {p["pid"]: p["cputime"] for p in procs}

        no_log, ended_name = self._scan_logs()
        cpu_active = bool(active_pids)
        was_running = self._running
        event = None

        if not self._primed:
            # first sample: just prime baselines, never signal
            self._primed = True
        elif not self._running:
            if cpu_active or no_log:
                self._running = True
                self._quiet = 0
                self._active_pids = active_pids
                self._refresh_hash_names()
                name = None
                for p in procs:
                    if p["pid"] in active_pids and p["hash"] in self._hash_names:
                        name = self._hash_names[p["hash"]]
                        break
                self._scenario = name
                event = "started"
        else:
            self._active_pids = active_pids or self._active_pids
            if ended_name is not None:
                self._running = False
                self._scenario = ended_name  # final, accurate name
                event = "ended"
            elif cpu_active or no_log:
                self._quiet = 0
            else:
                self._quiet += 1
                if self._quiet >= QUIET_SAMPLES_TO_END or not opened:
                    self._running = False
                    event = "ended"

        return {
            "open": opened,
            "running": self._running,
            "scenario": self._scenario,
            "event": event,
            "ended_name": ended_name,
            "active_pids": list(self._active_pids),
            "was_running": was_running,
        }
