"""Windows Task Scheduler integration.

- list_tasks(): read all non-Microsoft scheduled tasks via schtasks CSV
- create_task(): schedule `OrchardRPAWorker.exe --enqueue "<scenario>"` at a
  day/time — the enqueued run flows through the normal cloud task pipeline
  (status + live logs in the dashboard)
- delete_task(): remove one of OUR tasks (prefix guard — never system tasks)
- sync(): push the current snapshot into the cloud win_tasks table
"""
import csv
import io
import logging
import subprocess

from . import config as cfgmod
from .cloud import Cloud, utcnow

log = logging.getLogger("worker")

OURS_PREFIX = "OrchardRPA-"          # our dashboard-created tasks
DAY_CODES = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]  # 0=Sun (JS convention)

CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW


def _schtasks(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks"] + args, capture_output=True, text=True,
                          timeout=60, creationflags=CREATE_NO_WINDOW)


def list_tasks() -> list[dict]:
    """All scheduled tasks except the \\Microsoft\\ tree."""
    r = _schtasks(["/query", "/fo", "csv", "/v"])
    if r.returncode != 0:
        raise RuntimeError(f"schtasks /query failed: {r.stderr.strip()[:200]}")

    tasks: list[dict] = []
    seen: set[str] = set()
    for row in csv.DictReader(io.StringIO(r.stdout)):
        name = (row.get("TaskName") or "").strip()
        if not name or name == "TaskName":          # repeated header lines
            continue
        if name.startswith("\\Microsoft\\") or name in seen:
            continue
        seen.add(name)
        sched_parts = [row.get("Schedule Type"), row.get("Days"),
                       row.get("Start Time")]
        tasks.append({
            "task_name":   name,
            "next_run":    (row.get("Next Run Time") or "").strip() or None,
            "last_run":    (row.get("Last Run Time") or "").strip() or None,
            "last_result": (row.get("Last Result") or "").strip() or None,
            "status":      (row.get("Status") or "").strip() or None,
            "schedule":    " ".join(p.strip() for p in sched_parts if p and p.strip()) or None,
            "task_to_run": (row.get("Task To Run") or "").strip() or None,
            "is_ours":     name.lstrip("\\").startswith(OURS_PREFIX),
        })
    return tasks


def create_task(name: str, scenario: str, days: list[int], time_hhmm: str) -> str:
    """Create a Windows scheduled task that enqueues a cloud task for this PC."""
    safe = "".join(c for c in name if c.isalnum() or c in "-_ ").strip()
    if not safe:
        raise ValueError("Invalid task name")
    task_name = OURS_PREFIX + safe

    exe = cfgmod.exe_path()
    if cfgmod.is_frozen():
        tr = f'"{exe}" --enqueue "{scenario}"'
    else:  # dev mode: python worker.py --enqueue ...
        import sys
        tr = f'"{sys.executable}" "{exe}" --enqueue "{scenario}"'

    day_list = ",".join(DAY_CODES[d] for d in sorted(set(days)) if 0 <= d <= 6)
    if not day_list:
        raise ValueError("No days selected")

    r = _schtasks(["/create", "/tn", task_name, "/tr", tr,
                   "/sc", "weekly", "/d", day_list, "/st", time_hhmm, "/f"])
    if r.returncode != 0:
        raise RuntimeError(f"schtasks /create failed: {r.stderr.strip()[:200]}")
    log.info("Windows task created: %s (%s at %s)", task_name, day_list, time_hhmm)
    return task_name


def delete_task(task_name: str):
    """Delete one of OUR tasks only — guard against touching system tasks."""
    if not task_name.lstrip("\\").startswith(OURS_PREFIX):
        raise ValueError(f"Refusing to delete non-{OURS_PREFIX} task: {task_name}")
    r = _schtasks(["/delete", "/tn", task_name, "/f"])
    if r.returncode != 0:
        raise RuntimeError(f"schtasks /delete failed: {r.stderr.strip()[:200]}")
    log.info("Windows task deleted: %s", task_name)


def sync(cloud: Cloud) -> dict:
    """Replace this worker's win_tasks rows with a fresh snapshot."""
    tasks = list_tasks()
    now = utcnow()
    rows = [{**t, "worker_id": cloud.cfg.worker_id, "updated_at": now} for t in tasks]
    cloud.replace_win_tasks(rows)
    log.info("Windows scheduler sync: %d task(s) reported", len(rows))
    return {"total": len(rows), "ours": sum(1 for t in tasks if t["is_ours"])}
