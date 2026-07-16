"""Scenario folder scan — reports local .rks files to the cloud library.

Default scan targets the OneDrive Scenarios folder (names only — path
resolved per PC at runtime). A custom-folder scan stores each file's full
path on the scenario row; a literal {USERNAME} in the given path is kept
in the stored path so it still resolves per-PC at run time.
"""
import logging
import os
import re
from pathlib import Path

from .cloud import Cloud

log = logging.getLogger("worker")


def _resolve_username(path: str, username: str) -> str:
    return re.sub(r"(?i)\{\s*username\s*\}", username, path)


def scan(cloud: Cloud, cfg, folder: str | None = None) -> dict:
    if folder is None:
        # default scan → this company's folder (refreshed from the cloud so
        # dashboard folder changes take effect without a worker restart)
        co_folder = cloud.company_folder()
        if co_folder:
            cfg.scenarios_folder = co_folder
    raw = (folder or cfg.scenarios_folder).strip().rstrip("\\/")
    resolved = _resolve_username(raw, cfg.username)

    if not os.path.isdir(resolved):
        msg = f"Folder not found on {cfg.username}: {resolved}"
        log.error("Scan error: %s", msg)
        return {"error": msg, "found": [], "total": 0, "folder": resolved}

    names = sorted((p.stem for p in Path(resolved).glob("*.rks")), key=str.lower)
    if folder:
        # Custom folder → store full path per scenario (placeholder preserved)
        rows = [{"name": n, "path": os.path.join(raw, n + ".rks")} for n in names]
    else:
        rows = [{"name": n} for n in names]

    cloud.upsert_scenarios(rows)
    log.info("Scan: %d scenario(s) reported from %s", len(names), resolved)
    return {"error": None, "found": names, "total": len(names), "folder": resolved}
