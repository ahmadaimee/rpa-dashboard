"""
RPA-Bot Worker — cloud edition
==============================

First run (double-click the exe): asks for a pairing code, registers with
Supabase, installs a Task Scheduler logon task, and starts hidden with a
system-tray icon (Open Dashboard / Exit).

    RPA-Bot.exe                first-run setup / start-or-re-register menu
    RPA-Bot.exe --background   the hidden worker loop (set by Task Scheduler)
    RPA-Bot.exe --uninstall    remove autostart + stop + clear registration

Success/Failed: RkScenarioManager.exe /RUN exits 0 → success, non-zero →
failed. Full stdout is streamed to the cloud task_logs table.
"""
import argparse
import logging
import platform
import sys
import threading
import time

from version import __version__
import embedded
from core import config as cfgmod
from core import installer, scanner, heartbeat, updater, tray
from core.cloud import Cloud
from core.commands import CommandHandler
from core.runner import Runner

POLL_INTERVAL = 2  # s — task pickup cadence

log = logging.getLogger("worker")


def _setup_logging(to_file: bool):
    logging.basicConfig(
        level=logging.INFO,
        format=f"[%(asctime)s] [{cfgmod.USERNAME}] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # httpx logs every request at INFO — floods the worker log
    for noisy in ("httpx", "httpcore", "gotrue", "postgrest"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if to_file:
        try:
            from logging.handlers import RotatingFileHandler
            cfgmod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(cfgmod.LOG_FILE, maxBytes=2_000_000,
                                     backupCount=2, encoding="utf-8")
            fh.setFormatter(logging.Formatter(
                f"[%(asctime)s] [{cfgmod.USERNAME}] %(levelname)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"))
            logging.getLogger().addHandler(fh)
        except Exception:
            pass


def _hide_console():
    """Hide our console window in --background mode (exe is a console build
    so the first-run pairing prompt works; background runs go invisible)."""
    if platform.system() == "Windows":
        try:
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        except Exception:
            pass


def _set_console_title():
    if platform.system() == "Windows":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(f"RPA Agent [{cfgmod.USERNAME}]")
        except Exception:
            pass


def parse_args():
    p = argparse.ArgumentParser(description="RPA Agent Worker (cloud)")
    p.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--uninstall", action="store_true", help="Remove autostart and stop the worker")
    p.add_argument("--quiet", action="store_true", help="No prompts (used by the setup uninstaller)")
    p.add_argument("--enqueue", metavar="SCENARIO",
                   help="Insert a cloud task for this PC and exit "
                        "(used by dashboard-created Windows scheduled tasks)")
    return p.parse_args()


def worker_loop(cfg: cfgmod.Config):
    cloud = Cloud(cfg)
    # Never die on startup because the network/cloud is unavailable — a
    # startup crash left workers offline until the next logon.
    while True:
        try:
            cloud.sign_in()
            break
        except Exception as e:
            log.warning("Startup sign-in failed (%s) — retrying in 30s", str(e)[:150])
            time.sleep(30)
    try:
        co_folder = cloud.company_folder()
        if co_folder:
            cfg.scenarios_folder = co_folder
            log.info("Using company scenarios folder: %s", co_folder)
    except Exception:
        pass
    cloud.cleanup_orphan_running()
    runner = Runner(cloud, cfg)
    cmds = CommandHandler(cloud, cfg, runner)

    cloud.set_status("idle")
    tray.start(cloud, cfg, __version__, embedded.DASHBOARD_URL, runner)
    scanner.scan(cloud, cfg)  # auto-scan on startup
    try:
        from core import winsched
        winsched.sync(cloud)  # report Windows scheduled tasks on startup
    except Exception as e:
        log.debug("Initial winsched sync failed: %s", e)
    heartbeat.start(cloud, runner, __version__)
    cmds.start()

    log.info("Worker %s online (v%s)", cfg.worker_id, __version__)

    disabled_logged = False
    tick = 0
    while not cmds.shutdown.is_set():
        try:
            tick += 1
            # Auto-update: shortly after start, then every ~30 min, or on
            # dashboard command — only while idle (never mid-scenario).
            if (tick % 900 == 5 or cmds.update_requested) and not runner.current_task_id:
                cmds.update_requested = False
                updater.check_and_apply(cloud, __version__)
            # Respect the dashboard 'enabled' switch (checked every ~30 s)
            if tick % 15 == 1:
                if not cloud.worker_enabled():
                    if not disabled_logged:
                        log.warning("Worker disabled from dashboard — idling")
                        disabled_logged = True
                    time.sleep(POLL_INTERVAL)
                    continue
                disabled_logged = False

            # Keyence runs one automation at a time — while a run started
            # directly on the PC is active, hold the cloud queue.
            if heartbeat.external_task_id is not None or heartbeat.monitor._running:
                time.sleep(POLL_INTERVAL)
                continue

            task = cloud.claim_next_task()
            if task:
                log.info("Running task %s (%s)", task["id"], task["scenario_name"])
                runner.run(task)
        except Exception as e:
            log.exception("Loop error: %s", e)
        time.sleep(POLL_INTERVAL)


def main():
    args = parse_args()

    if args.uninstall:
        _setup_logging(to_file=False)
        cfg = cfgmod.load()
        cloud = None
        if cfg:
            try:
                cloud = Cloud(cfg)
                cloud.sign_in()
            except Exception:
                cloud = None
        installer.uninstall(cloud)
        if not args.quiet:
            input("  Press Enter to close...")
        return

    cfg = cfgmod.load()

    # ── Enqueue mode (fired by a Windows scheduled task) ────
    if args.enqueue:
        _hide_console()
        _setup_logging(to_file=True)
        if not cfg:
            log.error("--enqueue: no config — run the installer first")
            return
        cloud = Cloud(cfg)
        try:
            cloud.sign_in()
        except Exception as e:
            log.error("--enqueue: sign-in failed: %s", e)
            return
        ok = cloud.enqueue_task(args.enqueue)
        log.info("--enqueue '%s' → %s", args.enqueue, "queued" if ok else "FAILED")
        return

    # ── Background mode (Task Scheduler / relaunch) ─────────
    if args.background:
        _hide_console()
        _setup_logging(to_file=True)
        if not cfg:
            log.error("No config found — run the installer first (double-click the exe)")
            return
        if installer.is_already_running():
            log.info("Another worker instance is already running — exiting")
            return
        installer.acquire_instance_lock()
        _set_console_title()
        # Self-repair the autostart task on every background start — a missing
        # task broke the post-update restart on a worker PC once.
        try:
            installer.install_startup_task()
        except Exception as e:
            log.warning("Startup-task self-repair failed: %s", e)
        worker_loop(cfg)
        return

    # ── Interactive (double-click) ──────────────────────────
    _setup_logging(to_file=False)
    if cfg and installer.is_already_running():
        print()
        print("  ✅ RPA Agent is already running in the background.")
        print(f"  This exe: v{__version__}")
        print()
        print("  [Enter] Close   [R] Upgrade — restart the worker using THIS exe")
        if input("  Choice: ").strip().lower() == "r":
            print("  Stopping old worker...")
            installer.kill_running_worker()
            print("  Updating startup task to this exe and restarting...")
            installer.install_startup_task()
            installer.relaunch_background()
            time.sleep(3)
            print(f"  ✅ Worker restarted on v{__version__}.")
            time.sleep(2)
        return

    if cfg:
        print()
        print("  RPA Agent — this PC is already registered.")
        print(f"  Registered as: {cfg.display_name} ({cfg.username})")
        print()
        print("  [Enter] Start worker")
        print("  [R]     Re-register with a new pairing code (clears old registration)")
        print("  [U]     Uninstall from this PC")
        choice = input("  Choice: ").strip().lower()

        if choice == "u":
            installer.uninstall(None)
            input("  Press Enter to close...")
            return
        if choice == "r":
            print("  Clearing old registration...")
            installer.clear_registration()
            print("  ⚠  Old entry: remove this PC's previous row in the dashboard")
            print("     Settings tab (it will show as permanently offline).")
            installer.interactive_install(embedded.SUPABASE_URL, embedded.SUPABASE_ANON_KEY, __version__)
            return

        # Default: start worker
        print("  Starting worker...")
        installer.install_startup_task()
        installer.relaunch_background()
        time.sleep(3)
        print("  ✅ Worker started in the background (check the tray icon).")
        time.sleep(2)
        return

    installer.interactive_install(embedded.SUPABASE_URL, embedded.SUPABASE_ANON_KEY, __version__)


if __name__ == "__main__":
    main()
