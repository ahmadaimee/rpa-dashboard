"""Keyence license health — surface verification failures before they block runs.

Keyence RK-10 floating licenses re-verify against the license server and
stop running scenarios once verification has failed past the grace window
(~14 days). Every attempt is logged as one JSON file in
%LOCALAPPDATA%\\KEYENCE\\RkScenarioManager\\Log\\License\\
<yyyyMMddHHmmssfff>_<guid>.json with "Status" and "Detail" fields:

  success:  Status "Get License" with empty Detail
  failure:  Status "Failed to get license" / "Conflict",
            reason in Detail (server unreachable, license used by another
            user/device, TooManyUsers, HaspNotFound, ...)

health() reads those logs and reports ok / warning / error so the dashboard
can warn *before* the grace window runs out instead of after runs start
silently failing.
"""
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("worker")

LICENSE_LOG_DIR = (Path(os.environ.get("LOCALAPPDATA", ""))
                   / "KEYENCE" / "RkScenarioManager" / "Log" / "License")

WARN_AFTER_DAYS = 10     # no successful verification this long → warning
FAIL_AFTER_DAYS = 14     # Keyence's offline grace window → error
MAX_FILES = 500          # newest log files to consider per scan
CACHE_SECS = 300         # health() rescans at most this often

_STATUS_RE = re.compile(r'"Status"\s*:\s*"([^"]+)"')
_DETAIL_RE = re.compile(r'"Detail"\s*:\s*"((?:[^"\\]|\\.)*)"')
_FAIL_STATUSES = {"failed to get license", "conflict"}

_cache: dict | None = None
_cache_at = 0.0


def _file_ts(name: str) -> float | None:
    """Filenames start with yyyyMMddHHmmssfff (local time)."""
    m = re.match(r"(\d{17})_", name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S%f").timestamp()
    except ValueError:
        return None


def _clean_detail(detail: str) -> str:
    text = detail.replace("\\r\\n", "\n").replace('\\"', '"')
    # first meaningful line, minus the trailing "License server: ..." blurb
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()
             and not ln.strip().lower().startswith(("license server", "licensed to", "contact your it"))]
    return (lines[0] if lines else text.strip())[:300]


def health(force: bool = False) -> dict:
    """{"status": ok|warning|error|unknown, "last_verified": iso|None,
        "days_since": float|None, "error": str|None}"""
    global _cache, _cache_at
    if not force and _cache is not None and time.time() - _cache_at < CACHE_SECS:
        return _cache
    _cache_at = time.time()
    _cache = _scan()
    return _cache


def _scan() -> dict:
    out = {"status": "unknown", "last_verified": None, "days_since": None, "error": None}
    if not LICENSE_LOG_DIR.is_dir():
        return out
    try:
        files = sorted(
            (f for f in LICENSE_LOG_DIR.iterdir()
             if f.is_file() and _file_ts(f.name) is not None),
            key=lambda f: f.name, reverse=True)[:MAX_FILES]
    except Exception as e:
        log.debug("license log listing error: %s", e)
        return out

    last_success_ts = None
    failures_since: list[tuple[float, str]] = []   # (ts, detail) newer than last success
    for f in files:
        try:
            text = f.read_text(encoding="utf-8-sig", errors="ignore")
        except Exception:
            continue
        sm = _STATUS_RE.search(text)
        if not sm:
            continue
        status = sm.group(1).strip().lower()
        ts = _file_ts(f.name)
        if status == "get license":
            dm = _DETAIL_RE.search(text)
            if not (dm and dm.group(1).strip()):
                last_success_ts = ts
                break               # newest success found — older files irrelevant
        elif status in _FAIL_STATUSES:
            dm = _DETAIL_RE.search(text)
            failures_since.append((ts, _clean_detail(dm.group(1)) if dm else status))

    if last_success_ts is None:
        if failures_since:
            out["status"] = "error"
            out["error"] = failures_since[0][1]
        return out

    days = (time.time() - last_success_ts) / 86400
    out["last_verified"] = datetime.utcfromtimestamp(last_success_ts).strftime("%Y-%m-%dT%H:%M:%SZ")
    out["days_since"] = round(days, 1)

    if days > FAIL_AFTER_DAYS:
        out["status"] = "error"
        out["error"] = (failures_since[0][1] if failures_since
                        else f"License not verified for {int(days)} days — runs will be blocked")
    elif failures_since:
        out["status"] = "warning"
        out["error"] = failures_since[0][1]
    elif days > WARN_AFTER_DAYS:
        out["status"] = "warning"
        out["error"] = f"License not verified for {int(days)} days (grace ends at {FAIL_AFTER_DAYS})"
    else:
        out["status"] = "ok"
    return out
