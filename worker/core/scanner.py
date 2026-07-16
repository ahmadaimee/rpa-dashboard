"""Scenario folder scan — reports local .rks files to the cloud library.

Scans recursively (subfolders included). Default-scan files at the folder
root are reported by name only (path resolved per PC at runtime); files in
subfolders and all custom-folder scans store the full path on the scenario
row. A literal {USERNAME} in the folder is kept in stored paths so they
still resolve per-PC at run time.
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

    # Shallowest first: a top-level file wins over a same-named copy in a
    # subfolder, and duplicates are skipped — two rows with the same
    # (company, name) in one upsert batch are a Postgres error (21000).
    files = sorted(Path(resolved).rglob("*.rks"),
                   key=lambda p: (len(p.relative_to(resolved).parts), str(p).lower()))
    rows, names, seen = [], [], set()
    for p in files:
        if p.stem.lower() in seen:
            log.info("Scan: duplicate scenario name skipped: %s", p)
            continue
        seen.add(p.stem.lower())
        rel = p.relative_to(resolved)
        names.append(p.stem)
        if folder or rel.parent != Path("."):
            # Custom scan, or a file inside a subfolder → store the full path
            # (with any {USERNAME} placeholder preserved) so the runner can
            # find it; top-level default-folder files stay name-only.
            rows.append({"name": p.stem, "path": os.path.join(raw, str(rel))})
        else:
            rows.append({"name": p.stem})

    cloud.upsert_scenarios(rows)
    log.info("Scan: %d scenario(s) reported from %s", len(names), resolved)
    return {"error": None, "found": names, "total": len(names), "folder": resolved}
