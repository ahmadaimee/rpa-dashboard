import subprocess
import ctypes
import sys
import os
import time

RULE_NAME = "BLOCK KEYENCE RK-10 OUTBOUND"
APP_PATH = r"C:\Program Files\KEYENCE\RK-10\RkScenarioManager.exe"
PROCESS_NAME = "RkScenarioManager.exe"
WAIT_SECONDS = 30

def is_admin():
    """Check whether the script is running with Admin rights."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def set_firewall_rule(rule_name, enable):
    """Enable ('yes') or disable ('no') an outbound firewall rule."""
    state = "yes" if enable else "no"
    action = "Enabling" if enable else "Disabling"
    print(f"{action} outbound rule: '{rule_name}'...")

    # 'dir=out' is required for outbound rules
    set_command = f'netsh advfirewall firewall set rule name="{rule_name}" dir=out new enable={state}'
    result = subprocess.run(set_command, shell=True, capture_output=True, text=True)

    if "Updated" in result.stdout or result.returncode == 0:
        print(f"  -> Rule is now {'Enabled' if enable else 'Disabled'}.\n")
        return True
    else:
        print(f"  -> Failed: {result.stdout.strip()} {result.stderr.strip()}\n")
        return False

def launch_app(app_path):
    """Start the application without blocking the script."""
    print(f"Launching application: {app_path}")
    if not os.path.exists(app_path):
        print(f"  -> Error: Application not found at '{app_path}'\n")
        return False
    try:
        subprocess.Popen([app_path])
        print("  -> Application started.\n")
        return True
    except Exception as e:
        print(f"  -> Error launching application: {e}\n")
        return False

def kill_process(process_name):
    """Force-kill the given process if it is running."""
    print(f"Force-closing '{process_name}'...")
    result = subprocess.run(
        f'taskkill /F /IM "{process_name}"',
        shell=True, capture_output=True, text=True
    )
    if result.returncode == 0:
        print(f"  -> '{process_name}' has been force-closed.\n")
    else:
        # Non-zero return code usually means the process was not running
        print(f"  -> '{process_name}' was not running. {result.stdout.strip()}\n")

def run_sequence():
    # 1. Disable the firewall block rule
    set_firewall_rule(RULE_NAME, enable=False)

    # 2. Launch the application
    launch_app(APP_PATH)

    # 3. Wait for the configured time
    print(f"Waiting {WAIT_SECONDS} seconds...\n")
    time.sleep(WAIT_SECONDS)

    # 4. Kill the application
    kill_process(PROCESS_NAME)

    # 5. Re-enable the firewall block rule
    set_firewall_rule(RULE_NAME, enable=True)

    print("Sequence complete.")

if __name__ == "__main__":
    if is_admin():
        run_sequence()
    else:
        print("Requesting administrative privileges...")
        # Opens a UAC prompt when not running as admin
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{sys.argv[0]}"', None, 1)
