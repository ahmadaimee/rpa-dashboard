"""Scenario folder scan — reports local .rks files to the cloud library."""
import logging
import os
from pathlib import Path

from .cloud import Cloud

log = logging.getLogger("worker")


def scan(cloud: Cloud, cfg) -> dict:
    folder = cfg.resolved_scenarios_folder()
    if not os.path.isdir(folder):
        msg = f"Folder not found on {cfg.username}: {folder}"
        log.error("Scan error: %s", msg)
        return {"error": msg, "found": [], "total": 0}

    names = sorted((p.stem for p in Path(folder).glob("*.rks")), key=str.lower)
    cloud.upsert_scenarios(names)
    log.info("Scan: %d scenario(s) reported", len(names))
    return {"error": None, "found": names, "total": len(names)}
