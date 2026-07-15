"""Detect real Keyence RK-10 activity — distinguishing "app merely open"
from "a scenario is actually executing", and naming the scenario even when
a user starts it directly from the Keyence UI on the worker PC.

How: RK-10 writes %LOCALAPPDATA%\\KEYENCE\\RkScenarioManager\\Log\\<guid>\\
RunningLog.json per run, containing ScenarioPath/ScenarioHash/TriggerType,
with EndTime only once the run finishes. A recent run folder whose log is
missing or has no EndTime yet = run in progress. Scenario hashes seen in
past logs also let us name a run detected via the dotnet BuildCache
process command line.
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
RECENT_WINDOW_SECS = 36 * 3600   # ignore stale run folders (crashed runs etc.)

CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0

_SCEN_RE = re.compile(r'"ScenarioPath"\s*:\s*"((?:[^"\\]|\\.)*)"')
_HASH_RE = re.compile(r'"ScenarioHash"\s*:\s*"([0-9a-fA-F]+)"')
_END_RE  = re.compile(r'"EndTime"\s*:')


def rk_app_open() -> bool:
    """Is the Keyence app/engine process present at all?"""
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
    p = path_escaped.replace("\\\\", "\\")
    return Path(p).stem


def _hash_to_name_map(files: list[Path]) -> dict:
    m = {}
    for f in files[:200]:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
            h, s = _HASH_RE.search(text), _SCEN_RE.search(text)
            if h and s:
                m.setdefault(h.group(1).lower(), _scenario_name(s.group(1)))
        except Exception:
            continue
    return m


def _active_buildcache_hashes() -> list[str]:
    """Scenario hashes from KEYENCE dotnet runner command lines."""
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='dotnet.exe'\" "
             "| Select-Object -ExpandProperty CommandLine"],
            text=True, timeout=30, creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return []
    return re.findall(r"BuildCache\\[^\\]+\\([0-9a-fA-F]{32,})\\", out)


def rk_status() -> dict:
    """Returns {"open": bool, "running": bool, "scenario": str|None}."""
    opened = rk_app_open()
    result = {"open": opened, "running": False, "scenario": None}
    if not opened or not RK_LOG_DIR.is_dir():
        return result

    now = time.time()
    run_files = []
    try:
        for d in RK_LOG_DIR.iterdir():
            if not d.is_dir() or d.name.lower() == "license":
                continue
            f = d / "RunningLog.json"
            if f.exists():
                run_files.append(f)
            # Fresh run folder with no log yet → a run just started
            elif now - d.stat().st_mtime < RECENT_WINDOW_SECS:
                result["running"] = True
    except Exception as e:
        log.debug("rkdetect scan error: %s", e)
        return result

    run_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

    # A recent log without EndTime = run in progress, with its name
    for f in run_files[:20]:
        try:
            if now - f.stat().st_mtime > RECENT_WINDOW_SECS:
                break
            text = f.read_text(encoding="utf-8", errors="ignore")
            if not _END_RE.search(text):
                result["running"] = True
                s = _SCEN_RE.search(text)
                if s:
                    result["scenario"] = _scenario_name(s.group(1))
                return result
        except Exception:
            continue

    # Nameless in-progress run (folder without log) → try naming it from
    # the active dotnet BuildCache hash via historical logs
    if result["running"] and not result["scenario"]:
        hashes = _active_buildcache_hashes()
        if hashes:
            names = _hash_to_name_map(run_files)
            for h in hashes:
                if h.lower() in names:
                    result["scenario"] = names[h.lower()]
                    break

    return result
