"""Heartbeat thread — stamps last_seen / status / rk_running every 10 s."""
import logging
import threading
import time

from .cloud import Cloud
from .runner import Runner, rk_engine_running

log = logging.getLogger("worker")

HEARTBEAT_INTERVAL = 10


def start(cloud: Cloud, runner: Runner, app_version: str) -> threading.Thread:
    def loop():
        while True:
            try:
                status = "running" if runner.current_task_id else "idle"
                cloud.heartbeat(status, rk_engine_running(), app_version)
            except Exception as e:
                log.debug("Heartbeat error: %s", e)
            time.sleep(HEARTBEAT_INTERVAL)

    t = threading.Thread(target=loop, daemon=True, name="heartbeat")
    t.start()
    return t
