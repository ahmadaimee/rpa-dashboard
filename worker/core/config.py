"""Per-PC config + credentials in %LOCALAPPDATA%\\OrchardRPA\\config.json."""
import json
import logging
import os
import sys
from pathlib import Path

APP_NAME   = "RPA-Bot"
CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
PID_FILE    = CONFIG_DIR / "worker.pid"
LOG_FILE    = CONFIG_DIR / "worker.log"

USERNAME = os.environ.get("USERNAME", "UNKNOWN")


def _pin_ca_bundle():
    """PyInstaller onefile extracts certifi's CA bundle into a temp dir that
    Windows temp-cleanup can delete while the worker is running — after which
    every NEW TLS connection fails with [Errno 2] on cacert.pem (observed in
    production). Copy the bundle to our stable config dir and point certifi,
    httpx and requests at it. Must run before httpx is imported."""
    try:
        import shutil
        import certifi
        stable = CONFIG_DIR / "cacert.pem"
        src = certifi.where()
        if os.path.isfile(src):
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            if not stable.exists() or stable.stat().st_size != os.path.getsize(src):
                shutil.copyfile(src, stable)
        if stable.exists():
            os.environ["SSL_CERT_FILE"] = str(stable)
            os.environ["REQUESTS_CA_BUNDLE"] = str(stable)
            certifi.where = lambda: str(stable)
    except Exception:
        pass


_pin_ca_bundle()

DEFAULT_RK_EXE = r"C:\Program Files\KEYENCE\RK-10\RkScenarioManager.exe"
DEFAULT_SCENARIOS_FOLDER = (
    r"C:\Users\{USERNAME}\Orchard Medical Management"
    r"\Automations-PatriotPay - Documents\Scenarios"
)

log = logging.getLogger("worker")


class Config:
    def __init__(self, data: dict):
        self.supabase_url     = data["supabase_url"]
        self.anon_key         = data["anon_key"]
        self.email            = data["email"]
        self.password         = data["password"]
        self.worker_id        = data["worker_id"]
        self.username         = data.get("username", USERNAME)
        self.display_name     = data.get("display_name", USERNAME)
        self.rk_exe           = data.get("rk_exe") or DEFAULT_RK_EXE
        self.scenarios_folder = data.get("scenarios_folder") or DEFAULT_SCENARIOS_FOLDER

    def resolved_scenarios_folder(self) -> str:
        return self.scenarios_folder.replace("{USERNAME}", self.username)


def load() -> Config | None:
    try:
        if CONFIG_FILE.exists():
            return Config(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception as e:
        log.error("Config load failed: %s", e)
    return None


def save(data: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def exe_path() -> str:
    """Path of the running program — the frozen .exe, or worker.py via python."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return str(Path(sys.argv[0]).resolve())


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))
