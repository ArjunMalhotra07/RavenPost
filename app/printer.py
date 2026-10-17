"""Safe wrappers around CUPS CLI tools (lp, lpstat, cancel, cupsenable, cupsaccept)."""

import asyncio
import logging
import re
import subprocess
from pathlib import Path

from app.config import PRINTER_NAME, UPLOAD_DIR, JOB_TIMEOUT, JOB_POLL_INTERVAL

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


def _cancel_all():
    """Cancel all jobs for our printer. Used for post-print cleanup since CAPT
    leaves ghost jobs in the queue even after successful prints."""
    try:
        result = _run(["cancel", "-a", PRINTER_NAME])
        log.info("cancel -a %s: rc=%d stderr=%s", PRINTER_NAME, result.returncode, result.stderr.strip())
    except subprocess.TimeoutExpired:
        log.warning("cancel -a %s timed out", PRINTER_NAME)


def _recover_printer():
    """Re-enable the printer and make it accept jobs after a stuck job."""
    for cmd_name, cmd in [
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
    """Submit a job, wait for it to complete or timeout, and recover if stuck.

    Returns (success: bool, message: str).
    Holds _print_lock for the entire duration to prevent overlapping jobs.
    """

    if _print_lock.locked():
        return False, "Printer is busy with another job. Please wait and try again."

    async with _print_lock:
        # 1. Submit
        ok, job_id = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _submit_to_cups(
                file_path=file_path,
                copies=copies,
                page_range=page_range,
                number_up=number_up,
                orientation=orientation,
                scaling=scaling,
                scale_percent=scale_percent,
                paper_size=paper_size,
                sides=sides,
                margins=margins,
            ),
        )

        if not ok:
            return False, job_id  # job_id is the error message here

        log.info("Job %s submitted, monitoring (timeout=%ds)", job_id, JOB_TIMEOUT)

        # 2. Two-phase monitoring:
        #    Phase A: Wait for the printer to start processing (leave idle state).
        #             If it's still idle after 15s, the job may have gone through
        #             instantly or failed to reach the printer.
        #    Phase B: Wait for the printer to return to idle (done printing).
        #
        #    We check printer device status instead of the job queue because
        #    the CAPT driver leaves ghost jobs in lpstat -o after printing.

        elapsed = 0
        saw_printing = False
        phase_a_limit = 15       # seconds to wait for printing to start

        while elapsed < JOB_TIMEOUT:
            await asyncio.sleep(JOB_POLL_INTERVAL)
            elapsed += JOB_POLL_INTERVAL

            state = await asyncio.get_event_loop().run_in_executor(
                None, _get_printer_state,
            )
            log.info("Job %s poll: state=%s elapsed=%ds saw_printing=%s",
                     job_id, state, elapsed, saw_printing)

            if state == "printing":
                saw_printing = True

                # Check CUPS error log for CAPT crash signatures.
                # This catches stuck jobs fast regardless of PDF size —
                # a 100-page PDF legitimately in "printing" won't have
                # these errors, but a stuck CAPT driver will.
                has_error = await asyncio.get_event_loop().run_in_executor(
                    None, _check_capt_error, job_id,
                )
                if has_error:
                    log.warning("Job %s: CAPT error detected, recovering", job_id)
                    break
                continue

            if state == "idle":
                if saw_printing:
                    # Was printing, now idle → done
                    log.info("Job %s completed (printer idle) after %ds", job_id, elapsed)
                    await asyncio.get_event_loop().run_in_executor(None, _cancel_all)
                    await asyncio.get_event_loop().run_in_executor(None, _recover_printer)
                    return True, f"Print completed ({job_id})"
                elif elapsed >= phase_a_limit:
                    # Printer never left idle — job went through very fast
                    log.info("Job %s: printer stayed idle for %ds, assuming completed", job_id, elapsed)
                    await asyncio.get_event_loop().run_in_executor(None, _cancel_all)
                    await asyncio.get_event_loop().run_in_executor(None, _recover_printer)
                    return True, f"Print completed ({job_id})"

            if state == "disabled":
                log.warning("Job %s: printer disabled at %ds", job_id, elapsed)
                break

        # 3. Timeout, stuck, or disabled — recover
        log.warning("Job %s stuck after %ds, cancelling and recovering", job_id, elapsed)

        await asyncio.get_event_loop().run_in_executor(None, _cancel_all)
        await asyncio.sleep(2)
        await asyncio.get_event_loop().run_in_executor(None, _recover_printer)

        return False, (
            f"Print job {job_id} appeared stuck and was cancelled after {JOB_TIMEOUT}s. "
            f"Printer has been reset and is ready for the next job."
        )


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
