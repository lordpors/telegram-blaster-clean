from pathlib import Path
import sys
import types

# Compatibility mode: when Railway's Root Directory is set to ``app``,
# Uvicorn imports this file as top-level ``main``. Register the current
# directory as the ``app`` package so existing absolute imports still work.
if __package__ in (None, ""):
    package = types.ModuleType("app")
    package.__path__ = [str(Path(__file__).resolve().parent)]
    package.__package__ = "app"
    sys.modules.setdefault("app", package)


from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from sqlalchemy import text

from app.database import Base, SessionLocal, engine
from app.models import BlastJob, BlastRecipient, TelegramAccount  # noqa: F401
from app.services.blast_manager import blast_manager

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

Base.metadata.create_all(bind=engine)

# Migrasi kolom baru ke DB yang sudah ada (aman dijalankan berkali-kali)
with engine.connect() as _conn:
    try:
        _conn.execute(text("ALTER TABLE blast_jobs ADD COLUMN delay_max_seconds REAL"))
        _conn.commit()
    except Exception:
        pass  # Kolom sudah ada, tidak masalah

app = FastAPI(title="Telegram Blaster - miawjugabisa.com")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

from app.routers import dashboard, telegram, scraper, wa

app.include_router(dashboard.router)
app.include_router(telegram.router)
app.include_router(scraper.router)
app.include_router(wa.router)


@app.on_event("startup")
async def resume_jobs_after_restart():
    # Semua akun yang memiliki session valid dapat dipilih per job/tab.
    with SessionLocal() as db:
        db.query(TelegramAccount).filter(TelegramAccount.session_str.isnot(None)).update(
            {TelegramAccount.is_active: 1}, synchronize_session=False
        )
        db.commit()
    await blast_manager.resume_incomplete_jobs()


@app.get("/")
def root_redirect():
    return RedirectResponse(url="/dashboard")
