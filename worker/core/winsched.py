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

OURS_PREFIX = "RPA-Bot-Sched-"       # our dashboard-created tasks
DAY_CODES = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]  # 0=Sun (JS convention)

CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW


def _schtasks(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks"] + args, capture_output=True, text=True,
                          timeout=60, creationflags=CREATE_NO_WINDOW)


def _fail(op: str, r: subprocess.CompletedProcess):
    err = (r.stderr or r.stdout or "").strip()[:200]
    if "denied" in err.lower():
        err += (" — Hint: this PC restricts Task Scheduler for this account. "
                "Run RPA-Bot once as administrator, or use a Cloud Schedule "
                "(single scenario) instead — it needs no Windows permissions.")
    raise RuntimeError(f"schtasks {op} failed: {err}")


def _is_keyence_task(name: str, task_to_run: str) -> bool:
    """Only Keyence/RPA-related tasks are reported to the dashboard."""
    if name.lstrip("\\").startswith(OURS_PREFIX):
        return True
    t = (task_to_run or "").lower()
    return ("rkscenariomanager" in t or ".rks" in t
            or "rpa-bot" in t or "keyence" in t)


def list_tasks() -> list[dict]:
    """Keyence-related scheduled tasks only (ours + anything launching RK)."""
    r = _schtasks(["/query", "/fo", "csv", "/v"])
    if r.returncode != 0:
        _fail("/query", r)

    tasks: list[dict] = []
    seen: set[str] = set()
    for row in csv.DictReader(io.StringIO(r.stdout)):
        name = (row.get("TaskName") or "").strip()
        if not name or name == "TaskName":          # repeated header lines
            continue
        if name.startswith("\\Microsoft\\") or name in seen:
            continue
        if not _is_keyence_task(name, row.get("Task To Run") or ""):
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
        _fail("/create", r)
    log.info("Windows task created: %s (%s at %s)", task_name, day_list, time_hhmm)
    return task_name


def _guard_ours(task_name: str, verb: str):
    if not task_name.lstrip("\\").startswith(OURS_PREFIX):
        raise ValueError(f"Refusing to {verb} non-{OURS_PREFIX} task: {task_name}")


def delete_task(task_name: str):
    """Delete one of OUR tasks only — guard against touching system tasks."""
    _guard_ours(task_name, "delete")
    r = _schtasks(["/delete", "/tn", task_name, "/f"])
    if r.returncode != 0:
        _fail("/delete", r)
    log.info("Windows task deleted: %s", task_name)


def update_task(task_name: str, name: str, scenario: str,
                days: list[int], time_hhmm: str) -> str:
    """Replace one of OUR tasks with new settings (delete + recreate)."""
    _guard_ours(task_name, "update")
    delete_task(task_name)
    return create_task(name, scenario, days, time_hhmm)


def toggle_task(task_name: str, enable: bool):
    """Enable/disable one of OUR tasks."""
    _guard_ours(task_name, "toggle")
    r = _schtasks(["/change", "/tn", task_name, "/enable" if enable else "/disable"])
    if r.returncode != 0:
        _fail("/change", r)
    log.info("Windows task %s: %s", "enabled" if enable else "disabled", task_name)


def sync(cloud: Cloud) -> dict:
    """Replace this worker's win_tasks rows with a fresh snapshot."""
    tasks = list_tasks()
    now = utcnow()
    rows = [{**t, "worker_id": cloud.cfg.worker_id, "updated_at": now} for t in tasks]
    cloud.replace_win_tasks(rows)
    log.info("Windows scheduler sync: %d task(s) reported", len(rows))
    return {"total": len(rows), "ours": sum(1 for t in tasks if t["is_ours"])}
