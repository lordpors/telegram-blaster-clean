from pathlib import Path
from datetime import datetime
import os
import secrets
import sys
import types
from urllib.parse import quote

# Compatibility mode: when Railway's Root Directory is set to ``app``,
# Uvicorn imports this file as top-level ``main``. Register the current
# directory as the ``app`` package so existing absolute imports still work.
if __package__ in (None, ""):
    package = types.ModuleType("app")
    package.__path__ = [str(Path(__file__).resolve().parent)]
    package.__package__ = "app"
    sys.modules.setdefault("app", package)


from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from sqlalchemy import func, or_, text

from app.auth import AuthenticationRequired, session_secret_for_middleware
from app.database import SessionLocal, engine
from app.migrations import initialize_database
from app.models import BlastJob, BlastRecipient, DeviceSession, InboxConversation, InboxMessage, TelegramAccount, User  # noqa: F401
from app.services.blast_manager import blast_manager
from app.services.inbox_manager import inbox_manager

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

initialize_database()

app = FastAPI(title="PorsLabs Telegram Blaster")
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret_for_middleware(),
    session_cookie="porslabs_session",
    max_age=60 * 60 * 24 * 30,
    same_site="lax",
    https_only=bool(os.getenv("RAILWAY_ENVIRONMENT")),
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(AuthenticationRequired)
async def authentication_required(request: Request, _exc: AuthenticationRequired):
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        next_path = quote(request.url.path, safe="/")
        return RedirectResponse(f"/login?next={next_path}", status_code=303)
    return JSONResponse({"detail": "Authentication required"}, status_code=401)


from app.routers import auth, dashboard, inbox, scraper, security, special, telegram
from app.services.sheet_blaster import sheet_blaster

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(inbox.router)
app.include_router(telegram.router)
app.include_router(scraper.router)
app.include_router(security.router)
app.include_router(special.router)


@app.on_event("startup")
async def resume_jobs_after_restart():
    await inbox_manager.start_all()
    await blast_manager.resume_incomplete_jobs()
    sheet_blaster.start()


@app.on_event("shutdown")
async def disconnect_inbox_accounts():
    await sheet_blaster.shutdown()
    await inbox_manager.shutdown()


@app.get("/")
def root_redirect(request: Request):
    destination = "/dashboard" if request.session.get("user_id") else "/login"
    return RedirectResponse(url=destination)


@app.get("/health", include_in_schema=False)
def healthcheck():
    """Readiness probe used by Railway before switching live traffic."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/api/office/account-stats", include_in_schema=False)
def office_account_stats(request: Request):
    expected = os.getenv("OFFICE_STATS_KEY", "")
    supplied = request.headers.get("x-office-key", "")
    if not expected or not secrets.compare_digest(expected, supplied):
        return JSONResponse({"detail": "Not found"}, status_code=404)

    with SessionLocal() as db:
        office_username = os.getenv("OFFICE_STATS_USERNAME", "").strip().casefold()
        owner = db.query(User).filter(
            func.lower(User.username) == office_username
            if office_username
            else func.lower(User.email) == os.getenv(
                "BOOTSTRAP_OWNER_EMAIL", "amelw778@gmail.com"
            ).strip().casefold()
        ).first()
        if not owner:
            return {"active": 0, "flood": 0, "total": 0}

        accounts = db.query(TelegramAccount).filter(TelegramAccount.user_id == owner.id)
        connected = accounts.filter(
            TelegramAccount.is_active == 1,
            TelegramAccount.session_str.isnot(None),
        )
        now = datetime.utcnow()
        return {
            "active": connected.filter(or_(
                TelegramAccount.blast_available_at.is_(None),
                TelegramAccount.blast_available_at <= now,
            )).count(),
            "flood": connected.filter(TelegramAccount.blast_available_at > now).count(),
            "total": accounts.count(),
        }
