"""Heartbeat thread — last_seen/status every 10 s + external-run tracking.

When a scenario is started directly on the PC (from the Keyence UI), the
RkMonitor detects it and we create a normal cloud task row with
source='external' so it appears in the dashboard Task Queue with a Stop
button. When the run ends, the row is closed (and renamed to the accurate
scenario name from Keyence's RunningLog).
"""
import logging
import threading
import time

from .cloud import Cloud, utcnow
from .runner import Runner
from .rkdetect import RkMonitor

log = logging.getLogger("worker")

SAMPLE_INTERVAL = 3       # rk detection cadence — changes push to the cloud instantly
HEARTBEAT_INTERVAL = 10   # regular last_seen stamp when nothing changed

# Shared so the command handler can stop external runs
monitor = RkMonitor()
external_task_id: str | None = None
_ext_lock = threading.Lock()


def set_external_stopped():
    """Called by the stop_task command handler after killing the run."""
    global external_task_id
    with _ext_lock:
        external_task_id = None
    monitor.force_end()  # report idle on the next heartbeat, not after 60 s


def _handle_external(cloud: Cloud, runner: Runner, rk: dict):
    global external_task_id
    if runner.current_task_id:
        return  # our own cloud task is running — already tracked normally

    with _ext_lock:
        if rk["event"] == "started" and external_task_id is None:
            name = rk["scenario"] or "Keyence scenario (started on PC)"
            task_id = cloud.insert_external_task(name)
            if task_id:
                external_task_id = task_id
                log.info("External Keyence run detected → task %s (%s)", task_id, name)
        elif rk["event"] == "ended" and external_task_id is not None:
            fields = {"status": "success", "finished_at": utcnow()}
            if rk.get("ended_name"):
                fields["scenario_name"] = rk["ended_name"]
            cloud.update_task(external_task_id, fields)
            log.info("External Keyence run finished → task %s", external_task_id)
            external_task_id = None
        elif rk["running"] and external_task_id is not None and rk.get("ended_name"):
            # a better name became available mid-tracking
            cloud.update_task(external_task_id, {"scenario_name": rk["ended_name"]})


def start(cloud: Cloud, runner: Runner, app_version: str) -> threading.Thread:
    def loop():
        last_push = 0.0
        last_state = None
        while True:
            try:
                rk = monitor.sample()
                status = "running" if (runner.current_task_id or rk["running"]) else "idle"
                state = (status, rk["running"], rk["open"], rk["scenario"])
                # Push immediately on any change; otherwise stamp every 10 s
                if (rk["event"] or state != last_state
                        or time.monotonic() - last_push >= HEARTBEAT_INTERVAL):
                    cloud.heartbeat(status, rk, app_version)
                    last_push = time.monotonic()
                    last_state = state
                _handle_external(cloud, runner, rk)
            except Exception as e:
                log.debug("Heartbeat error: %s", e)
            time.sleep(SAMPLE_INTERVAL)

    t = threading.Thread(target=loop, daemon=True, name="heartbeat")
    t.start()
    return t
