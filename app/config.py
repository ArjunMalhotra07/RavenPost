"""Application configuration loaded from environment / .env file."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the project root (one level above app/)
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


SECRET_KEY: str = _require("SECRET_KEY")
ADMIN_USERNAME: str = _require("ADMIN_USERNAME")
ADMIN_PASSWORD_HASH: str = _require("ADMIN_PASSWORD_HASH")

PRINTER_NAME: str = os.getenv("PRINTER_NAME", "LBP2900")
MAX_UPLOAD_BYTES: int = int(os.getenv("MAX_UPLOAD_BYTES", "20971520"))  # 20 MB

UPLOAD_DIR: Path = Path(os.getenv("UPLOAD_DIR", "uploads")).resolve()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

HOST: str = os.getenv("HOST", "127.0.0.1")
PORT: int = int(os.getenv("PORT", "8000"))

# Session cookie max-age in seconds (8 hours)
SESSION_MAX_AGE: int = 8 * 60 * 60

# Stale upload cleanup: delete files older than this many seconds
UPLOAD_TTL_SECONDS: int = 300  # 5 minutes

# How long to wait for a CUPS job to finish before declaring it stuck (seconds)
JOB_TIMEOUT: int = int(os.getenv("JOB_TIMEOUT", "90"))

# How often to poll lpstat while waiting for a job to complete (seconds)
JOB_POLL_INTERVAL: int = int(os.getenv("JOB_POLL_INTERVAL", "3"))

# If the printer stays in "printing" state longer than this, assume stuck (seconds)
# Set this a bit above your longest normal print time. A normal single-page PDF
# takes ~20s on the LBP2900, so 45s is generous.
JOB_STUCK_PRINTING: int = int(os.getenv("JOB_STUCK_PRINTING", "45"))
