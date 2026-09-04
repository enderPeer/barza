#!/usr/bin/env python3
"""Email server supervisor - manages the email server process."""
import subprocess
import sys
import time
import signal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVER_SCRIPT = ROOT / "email_server.py"
LOG_FILE = ROOT / "email_supervisor.log"


def log(msg):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def main():
    log("Email supervisor starting...")
    
    process = None
    restart_delay = 5
    
    try:
        while True:
            if process is None or process.poll() is not None:
                log("Starting email server...")
                try:
                    process = subprocess.Popen(
                        [sys.executable, str(SERVER_SCRIPT)],
                        cwd=str(ROOT),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True
                    )
                    log(f"Email server started (PID: {process.pid})")
                except Exception as e:
                    log(f"Failed to start email server: {e}")
                    time.sleep(restart_delay)
                    continue
            
            if process.stdout:
                try:
                    line = process.stdout.readline()
                    if line:
                        log(f"[Server] {line.rstrip()}")
                except Exception as e:
                    log(f"Error reading output: {e}")
            
            time.sleep(1)
            
    except KeyboardInterrupt:
        log("Supervisor shutting down...")
        if process:
            process.terminate()
            process.wait()
        log("Supervisor stopped.")


if __name__ == "__main__":
    main()
