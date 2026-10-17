"""FastAPI application — web interface for remote printing via CUPS."""

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

# Configure logging so printer.py log messages actually show up
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

from fastapi import FastAPI, Request, UploadFile, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.responses import PlainTextResponse
from PyPDF2 import PdfReader
import io

from app import config, auth, printer

# ---------------------------------------------------------------------------
# Stale upload cleanup
# ---------------------------------------------------------------------------

async def _cleanup_uploads():
    """Periodically delete upload files older than UPLOAD_TTL_SECONDS."""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        try:
            for f in config.UPLOAD_DIR.iterdir():
                if f.is_file() and (now - f.stat().st_mtime) > config.UPLOAD_TTL_SECONDS:
                    f.unlink(missing_ok=True)
        except OSError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_cleanup_uploads())
    yield
    task.cancel()

# ---------------------------------------------------------------------------
# App init
# ---------------------------------------------------------------------------

app = FastAPI(title="Print Server", docs_url=None, redoc_url=None, lifespan=lifespan)

# Rate limiter (keyed by IP)
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return PlainTextResponse("Too many requests. Try again later.", status_code=429)


# Templates & static files
_base = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_base / "templates"))
app.mount("/static", StaticFiles(directory=str(_base / "static")), name="static")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SESSION_COOKIE = "session"


def _get_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return auth.validate_session_token(token)


def _require_login(request: Request) -> str:
    user = _get_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def _csrf_token(request: Request) -> str:
    return auth.generate_csrf_token(request.cookies.get(SESSION_COOKIE, ""))


def _check_csrf(request: Request, csrf_token: str):
    session_token = request.cookies.get(SESSION_COOKIE, "")
    if not auth.validate_csrf_token(csrf_token, session_token):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


# ---------------------------------------------------------------------------
# Routes — Auth
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _get_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
@limiter.limit("5/minute")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if auth.verify_password(username, password):
        token = auth.create_session_token(username)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            secure=True,
            samesite="strict",
            max_age=config.SESSION_MAX_AGE,
        )
        return response
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Invalid credentials"},
        status_code=401,
    )


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ---------------------------------------------------------------------------
# Routes — Print
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    _require_login(request)
    status = printer.get_printer_status()
    busy = printer.is_printer_busy()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "csrf_token": _csrf_token(request),
        "printer_status": status,
        "printer_busy": busy,
        "max_upload_mb": config.MAX_UPLOAD_BYTES // (1024 * 1024),
        "result": None,
    })


@app.post("/print")
async def print_file(
    request: Request,
    csrf_token: str = Form(...),
    file: UploadFile = Form(...),
    copies: int = Form(1),
    page_range: str = Form(""),
    number_up: int = Form(1),
    orientation: str = Form("portrait"),
    scaling: str = Form("fit-printable"),
    scale_percent: int = Form(100),
    paper_size: str = Form("A4"),
    sides: str = Form("one-sided"),
    margins: str = Form("default"),
):
    _require_login(request)
    _check_csrf(request, csrf_token)

    # --- Validate uploaded file ---
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file uploaded")

    # Read file content (enforce size limit)
    contents = await file.read()
    if len(contents) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large")
    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    # Validate that it's actually a PDF
    if not contents[:5].startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="File is not a valid PDF")

    # Additional PDF validation — try to parse it
    try:
        reader = PdfReader(io.BytesIO(contents))
        _ = len(reader.pages)
    except Exception:
        raise HTTPException(status_code=400, detail="Corrupt or invalid PDF file")

    # Save with a random filename
    safe_name = f"{uuid.uuid4().hex}.pdf"
    dest = config.UPLOAD_DIR / safe_name
    dest.write_bytes(contents)

    # Submit to CUPS and monitor until completion or timeout
    try:
        success, message = await printer.submit_and_monitor(
            file_path=dest,
            copies=copies,
            page_range=page_range,
            number_up=number_up,
            orientation=orientation,
            scaling=scaling,
            scale_percent=scale_percent,
            paper_size=paper_size,
            sides=sides,
            margins=margins,
        )
    finally:
        # Always clean up the uploaded file
        dest.unlink(missing_ok=True)

    status = printer.get_printer_status()
    busy = printer.is_printer_busy()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "csrf_token": _csrf_token(request),
        "printer_status": status,
        "printer_busy": busy,
        "max_upload_mb": config.MAX_UPLOAD_BYTES // (1024 * 1024),
        "result": {"success": success, "message": message},
    })


# ---------------------------------------------------------------------------
# Routes — Job history
# ---------------------------------------------------------------------------

@app.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request):
    _require_login(request)
    jobs = printer.get_jobs()
    status = printer.get_printer_status()
    return templates.TemplateResponse("jobs.html", {
        "request": request,
        "jobs": jobs,
        "printer_status": status,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=config.HOST,
        port=config.PORT,
        log_level="info",
    )
