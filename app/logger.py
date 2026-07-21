"""
Application Logger
"""

from pathlib import Path
from datetime import datetime

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)


def _timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write(filename: str, level: str, message: str):

    logfile = LOG_DIR / filename

    with logfile.open("a", encoding="utf-8") as f:

        f.write(
            f"[{_timestamp()}] [{level}] {message}\n"
        )

    # Cetak juga ke stdout supaya kelihatan di log hosting (mis. menu
    # Logs di Render) -- file log di atas hilang tiap redeploy di
    # hosting ephemeral, dan tidak bisa dibaca dari dashboard.
    print(f"[{level}] {message}", flush=True)


def info(message: str):
    _write("telegram.log", "INFO", message)


def warning(message: str):
    _write("telegram.log", "WARNING", message)


def error(message: str):
    _write("error.log", "ERROR", message)


def startup(message: str):
    _write("startup.log", "STARTUP", message)


def parser(message: str):
    _write("parser.log", "PARSER", message)


def debug(message: str):
    _write("debug.log", "DEBUG", message)