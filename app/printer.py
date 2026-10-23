"""Safe wrappers around CUPS CLI tools (lp, lpstat, cancel, cupsenable, cupsaccept)."""

import asyncio
import logging
import re
import subprocess
from pathlib import Path

from app.config import PRINTER_NAME, UPLOAD_DIR, JOB_TIMEOUT, JOB_POLL_INTERVAL, JOB_STUCK_PRINTING

log = logging.getLogger("print-server")

# ---------------------------------------------------------------------------
# Print lock — prevents overlapping jobs while one is being monitored/recovered
# ---------------------------------------------------------------------------

_print_lock = asyncio.Lock()


def is_printer_busy():
    """Check if a print job is currently being processed."""
    return _print_lock.locked()


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------

def _validate_page_range(page_range: str):
    """Return sanitised page range or None if invalid.

    Accepts patterns like: 1  1-5  1,3,5  1-3,7-9
    """
    if not page_range or not page_range.strip():
        return None
    cleaned = page_range.strip()
    if re.fullmatch(r"(\d+(-\d+)?)(,\d+(-\d+)?)*", cleaned):
        return cleaned
    return None


VALID_SCALING = ("default", "fit-printable", "fit-paper", "custom")
VALID_PAPER_SIZES = ("A4", "Letter", "Legal", "A3", "A5")
VALID_SIDES = ("one-sided", "two-sided-long-edge", "two-sided-short-edge")
MARGIN_MAP = {
    "default": None,
    "none": 0,
    "small": 14,      # ~5mm
    "medium": 28,      # ~10mm
    "large": 42,       # ~15mm
}


# ---------------------------------------------------------------------------
# CUPS helpers
# ---------------------------------------------------------------------------

def _run(cmd, timeout=15):
    """Run a subprocess safely and return the CompletedProcess."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _get_printer_state():
    """Return the printer state as a string: 'idle', 'printing', 'error', 'disabled', or 'unknown'.

    lpstat -p output examples:
      "printer LBP2900 is idle.  enabled since ..."
      "printer LBP2900 now printing LBP2900-28.  enabled since ..."
      "printer LBP2900 disabled since ..."
    """
    try:
        result = _run(["lpstat", "-p", PRINTER_NAME])
        output = result.stdout.lower()
        if "now printing" in output:
            return "printing"
        if "idle" in output:
            return "idle"
        if "disabled" in output:
            return "disabled"
        return "unknown"
    except subprocess.TimeoutExpired:
        return "unknown"


def _check_capt_error(job_id: str) -> bool:
    """Check if the CUPS error log contains CAPT failure messages for this job.

    Looks for known CAPT crash signatures:
      - "Unable to send data to printer"
      - "CAPT: bad reply from printer"
      - "Backend usb returned status 1"

    Only checks the last 50 lines to avoid false positives from old errors.
    """
    # Extract the numeric job number from e.g. "LBP2900-36"
    job_num = job_id.rsplit("-", 1)[-1] if "-" in job_id else ""
    try:
        result = _run(["tail", "-50", "/var/log/cups/error_log"])
        for line in result.stdout.splitlines():
            # Only match lines about this specific job
            if job_num and f"[Job {job_num}]" not in line:
                continue
            lower = line.lower()
            if any(sig in lower for sig in (
                "unable to send data to printer",
                "bad reply from printer",
                "backend usb returned status",
            )):
                log.warning("CAPT error detected for job %s: %s", job_id, line.strip())
                return True
    except subprocess.TimeoutExpired:
        pass
    return False


def _recover_printer():
    """Cancel all jobs and re-enable the printer.

    Used after a stuck job is detected. The cancel of the stuck CAPT process
    is what actually resets the driver — this is the only recovery that works.
    """
    for cmd_name, cmd in [
        ("cancel -a", ["cancel", "-a", PRINTER_NAME]),
        ("cupsenable", ["cupsenable", PRINTER_NAME]),
        ("cupsaccept", ["cupsaccept", PRINTER_NAME]),
    ]:
        try:
            result = _run(cmd)
            log.info("%s %s: rc=%d stderr=%s", cmd_name, PRINTER_NAME, result.returncode, result.stderr.strip())
        except subprocess.TimeoutExpired:
            log.warning("%s %s timed out", cmd_name, PRINTER_NAME)


# ---------------------------------------------------------------------------
# Job submission
# ---------------------------------------------------------------------------

def _submit_to_cups(
    file_path: Path,
    copies: int = 1,
    page_range: str = "",
    number_up: int = 1,
    orientation: str = "portrait",
    scaling: str = "fit-printable",
    scale_percent: int = 100,
    paper_size: str = "A4",
    sides: str = "one-sided",
    margins: str = "default",
):
    """Submit a print job via `lp`. Returns (success, job_id_or_error)."""

    resolved = file_path.resolve()
    if not resolved.is_relative_to(UPLOAD_DIR):
        return False, "Invalid file path."
    if not resolved.is_file():
        return False, "File not found."

    copies = max(1, min(copies, 99))

    if number_up not in (1, 2, 4):
        number_up = 1

    if orientation not in ("portrait", "landscape"):
        orientation = "portrait"
    landscape_flag = "4" if orientation == "landscape" else "3"

    if scaling not in VALID_SCALING:
        scaling = "fit-printable"
    scale_percent = max(1, min(scale_percent, 999))

    if paper_size not in VALID_PAPER_SIZES:
        paper_size = "A4"

    if sides not in VALID_SIDES:
        sides = "one-sided"

    cmd = [
        "lp",
        "-d", PRINTER_NAME,
        "-n", str(copies),
        "-o", f"number-up={number_up}",
        "-o", f"orientation-requested={landscape_flag}",
        "-o", f"media={paper_size}",
        "-o", f"sides={sides}",
    ]

    if scaling == "fit-printable":
        cmd.extend(["-o", "fit-to-page=true"])
    elif scaling == "fit-paper":
        cmd.extend(["-o", "fit-to-page=true", "-o", "page-top=0", "-o", "page-bottom=0", "-o", "page-left=0", "-o", "page-right=0"])
    elif scaling == "custom":
        cmd.extend(["-o", f"natural-scaling={scale_percent}"])

    margin_pts = MARGIN_MAP.get(margins)
    if margin_pts is not None:
        for side in ("top", "bottom", "left", "right"):
            cmd.extend(["-o", f"page-{side}={margin_pts}"])

    validated_range = _validate_page_range(page_range)
    if validated_range:
        cmd.extend(["-o", f"page-ranges={validated_range}"])

    cmd.append(str(resolved))

    try:
        result = _run(cmd, timeout=30)
    except subprocess.TimeoutExpired:
        return False, "Print command timed out."

    if result.returncode != 0:
        return False, f"lp failed: {result.stderr.strip()}"

    match = re.search(r"request id is (\S+)", result.stdout)
    job_id = match.group(1) if match else None
    if not job_id:
        return False, "Could not parse job ID from lp output."

    return True, job_id


# ---------------------------------------------------------------------------
# Async: submit + monitor + recover
# ---------------------------------------------------------------------------

async def submit_and_monitor(
    file_path: Path,
    copies: int = 1,
    page_range: str = "",
    number_up: int = 1,
    orientation: str = "portrait",
    scaling: str = "fit-printable",
    scale_percent: int = 100,
    paper_size: str = "A4",
    sides: str = "one-sided",
    margins: str = "default",
):
    """Submit a job, monitor it, and auto-retry once if the CAPT driver hangs.

    The Canon CAPT driver has a known bug where every second print produces
    a blank page and hangs. This function detects the hang, recovers the
    printer, and automatically retries. The retry (being the "first print
    after recovery") always succeeds.

    Returns (success: bool, message: str).
    Holds _print_lock for the entire duration to prevent overlapping jobs.
    """

    if _print_lock.locked():
        return False, "Printer is busy with another job. Please wait and try again."

    async with _print_lock:
        submit_kwargs = dict(
            file_path=file_path, copies=copies, page_range=page_range,
            number_up=number_up, orientation=orientation, scaling=scaling,
            scale_percent=scale_percent, paper_size=paper_size,
            sides=sides, margins=margins,
        )

        # Try up to 2 times: original + 1 auto-retry after CAPT hang
        for attempt in range(1, 3):
            ok, job_id = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _submit_to_cups(**submit_kwargs),
            )

            if not ok:
                return False, job_id

            log.info("Job %s submitted (attempt %d/2), monitoring (timeout=%ds)",
                     job_id, attempt, JOB_TIMEOUT)

            result = await _monitor_job(job_id)

            if result == "completed":
                return True, f"Print completed ({job_id})"

            # Job stuck — recover and retry
            log.warning("Job %s stuck, recovering printer (attempt %d/2)", job_id, attempt)
            await asyncio.get_event_loop().run_in_executor(None, _recover_printer)
            await asyncio.sleep(3)  # brief settle after recovery

            if attempt == 1:
                log.info("Auto-retrying print after CAPT driver recovery")

        return False, (
            f"Print failed after 2 attempts. The printer has been reset "
            f"and is ready — please try again."
        )


async def _monitor_job(job_id):
    """Monitor a submitted job until it completes or is detected as stuck.

    Returns: 'completed' or 'stuck'
    """
    elapsed = 0
    saw_printing = False
    printing_for = 0
    phase_a_limit = 15

    while elapsed < JOB_TIMEOUT:
        await asyncio.sleep(JOB_POLL_INTERVAL)
        elapsed += JOB_POLL_INTERVAL

        state = await asyncio.get_event_loop().run_in_executor(
            None, _get_printer_state,
        )
        log.info("Job %s poll: state=%s elapsed=%ds printing_for=%ds",
                 job_id, state, elapsed, printing_for)

        if state == "printing":
            saw_printing = True
            printing_for += JOB_POLL_INTERVAL

            if printing_for >= JOB_STUCK_PRINTING:
                log.warning("Job %s stuck in printing for %ds", job_id, printing_for)
                return "stuck"
            continue

        if state == "idle":
            if saw_printing:
                log.info("Job %s completed (printer idle) after %ds", job_id, elapsed)
                return "completed"
            elif elapsed >= phase_a_limit:
                log.info("Job %s: printer stayed idle for %ds, assuming completed",
                         job_id, elapsed)
                return "completed"

        if state == "disabled":
            log.warning("Job %s: printer disabled at %ds", job_id, elapsed)
            return "stuck"

    log.warning("Job %s timed out after %ds", job_id, elapsed)
    return "stuck"


# ---------------------------------------------------------------------------
# Status / history (unchanged)
# ---------------------------------------------------------------------------

def get_jobs():
    """Return recent print jobs from lpstat."""
    try:
        result = _run(["lpstat", "-W", "all", "-o", PRINTER_NAME])
    except subprocess.TimeoutExpired:
        return []

    jobs = []
    for line in result.stdout.strip().splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 3:
            jobs.append({
                "id": parts[0],
                "user": parts[1],
                "size": parts[2],
                "date": parts[3] if len(parts) > 3 else "",
            })
    return jobs


def get_printer_status():
    """Return one-line printer status."""
    try:
        result = _run(["lpstat", "-p", PRINTER_NAME])
        return result.stdout.strip() or "Unknown"
    except subprocess.TimeoutExpired:
        return "Status check timed out"
