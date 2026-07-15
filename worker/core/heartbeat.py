"""Heartbeat thread — stamps last_seen / status / Keyence activity every 10 s.

rk_running now means "a scenario is actually executing" (via rkdetect), not
merely "the Keyence app is open" — that's rk_open. rk_scenario names the
scenario when it can be determined (including user-initiated runs).
"""
import logging
import threading
import time

from .cloud import Cloud
from .runner import Runner
from . import rkdetect

log = logging.getLogger("worker")

HEARTBEAT_INTERVAL = 10


def start(cloud: Cloud, runner: Runner, app_version: str) -> threading.Thread:
    def loop():
        while True:
            try:
                status = "running" if runner.current_task_id else "idle"
                rk = rkdetect.rk_status()
                cloud.heartbeat(status, rk, app_version)
            except Exception as e:
                log.debug("Heartbeat error: %s", e)
            time.sleep(HEARTBEAT_INTERVAL)

    t = threading.Thread(target=loop, daemon=True, name="heartbeat")
    t.start()
    return t
