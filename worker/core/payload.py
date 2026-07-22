"""Bundled maintenance scripts (payloads/) shipped inside the worker exe.

The Weekly Pass task runs `payloads/firewall.py`. A frozen build has no
python.exe next to it, so the script is executed by re-invoking our own exe
with the hidden `--run-script` flag: PyInstaller ships a full CPython, and
worker.py runs the file through runpy in that child process. Output is
streamed back exactly like a scenario run.
"""
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from .config import CONFIG_DIR, exe_path, is_frozen

# task source → bundled script file
SCRIPTS = {"weekly_pass": "firewall.py"}


def _bundle_dir() -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "payloads"
    return Path(__file__).resolve().parent.parent / "payloads"


def script_for(source: str) -> str | None:
    """Copy the bundled script to a stable folder and return its path.

    Copied out of the PyInstaller temp dir so a temp cleanup mid-run (see
    config._pin_ca_bundle) cannot pull the file out from under us.
    """
    name = SCRIPTS.get(source)
    if not name:
        return None
    src = _bundle_dir() / name
    if not src.is_file():
        return None
    dst_dir = CONFIG_DIR / "scripts"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / name
    try:
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copyfile(src, dst)
    except Exception:
        return str(src)
    return str(dst)


def command_for(script_path: str) -> list[str]:
    """Argv that runs the script with our own embedded interpreter."""
    if is_frozen():
        return [exe_path(), "--run-script", script_path]
    return [sys.executable, script_path]


def run_env() -> dict:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env
