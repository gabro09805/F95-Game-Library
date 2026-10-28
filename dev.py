import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STOP = threading.Event()


def source_snapshot():
    files = [
        path for path in ROOT.rglob("*.py")
        if not any(part in {".git", "__pycache__", ".venv", "venv"} for part in path.parts)
    ]
    return tuple(
        (str(path.relative_to(ROOT)), path.stat().st_mtime_ns, path.stat().st_size)
        for path in sorted(files)
    )


def request_stop(_signum, _frame):
    STOP.set()


def stop_child(child):
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def main():
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    os.environ["DEV_MODE"] = "1"
    snapshot = source_snapshot()
    child = subprocess.Popen([sys.executable, "-B", str(ROOT / "app.py")], cwd=ROOT)
    print("Modalità sviluppo attiva: modifiche Python ricaricate automaticamente.", flush=True)
    try:
        while not STOP.wait(1):
            current = source_snapshot()
            if current != snapshot:
                print("Modifica Python rilevata: riavvio del server...", flush=True)
                stop_child(child)
                snapshot = current
                child = subprocess.Popen([sys.executable, "-B", str(ROOT / "app.py")], cwd=ROOT)
            elif child.poll() is not None:
                print(f"Server terminato con codice {child.returncode}; in attesa di modifiche.", flush=True)
                while not STOP.wait(1):
                    current = source_snapshot()
                    if current != snapshot:
                        snapshot = current
                        child = subprocess.Popen([sys.executable, "-B", str(ROOT / "app.py")], cwd=ROOT)
                        break
    finally:
        stop_child(child)


if __name__ == "__main__":
    main()
