"""
Scraper router — pakai Telethon session yang sudah ada di DB
Tidak menyentuh tabel existing, data akun/blast aman
"""
import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat

from app.auth import get_current_user, verify_csrf
from app.database import get_db
from app.models import TelegramAccount, User

router = APIRouter()
APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

# ─── In-memory job store (reset saat Railway restart, itu normal) ───────────
_jobs: dict[str, dict] = {}
_bg_tasks: set = set()   # cegah GC pada asyncio.create_task
MAX_IN_MEMORY_JOBS = 20


def _render(request, template, ctx):
    ctx["request"] = request
    return templates.TemplateResponse(request, template, ctx)


def _prune_finished_jobs() -> None:
    """Bound retained contact sets so a long-lived worker cannot grow forever."""
    if len(_jobs) < MAX_IN_MEMORY_JOBS:
        return
    for old_job_id, old_job in list(_jobs.items()):
        if old_job.get("status") != "running":
            _jobs.pop(old_job_id, None)
            if len(_jobs) < MAX_IN_MEMORY_JOBS:
                break


# ─── GET /scraper ────────────────────────────────────────────────────────────
@router.get("/scraper", response_class=HTMLResponse)
async def scraper_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    accounts = (
        db.query(TelegramAccount)
        .filter(
            TelegramAccount.user_id == current_user.id,
            TelegramAccount.is_active == 1,
        )
        .order_by(TelegramAccount.created_at)
        .all()
    )
    return _render(request, "scraper.html", {
        "accounts": accounts,
        "job_id": None,
    })


# ─── POST /scraper/start ─────────────────────────────────────────────────────
@router.post("/scraper/start", response_class=HTMLResponse)
async def scraper_start(
    request: Request,
    account_id: int = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    accounts = (
        db.query(TelegramAccount)
        .filter(
            TelegramAccount.user_id == current_user.id,
            TelegramAccount.is_active == 1,
        )
        .order_by(TelegramAccount.created_at)
        .all()
    )
    account = (
        db.query(TelegramAccount)
        .filter(
            TelegramAccount.id == account_id,
            TelegramAccount.user_id == current_user.id,
        )
        .first()
    )

    if not account or not account.session_str:
        return _render(request, "scraper.html", {
            "accounts": accounts,
            "job_id": None,
            "error_message": "Akun tidak valid atau session kosong.",
        })

    _prune_finished_jobs()
    job_id = uuid.uuid4().hex
    _jobs[job_id] = {
        "status": "running",
        "contacts": set(),
        "log": ["⏳ Menyambung ke Telegram..."],
        "total": 0,
        "user_id": current_user.id,
    }

    # Jalankan scraping di background — tidak block HTTP response
    task = asyncio.create_task(
        _do_scrape(
            job_id,
            account.session_str,
            account.api_id,
            account.api_hash,
            account.label or account.phone,
        )
    )
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)

    return _render(request, "scraper.html", {
        "accounts": accounts,
        "job_id": job_id,
        "selected_account_id": account_id,
    })


# ─── Background scraping logic ───────────────────────────────────────────────
async def _do_scrape(job_id: str, session_str: str, api_id: int, api_hash: str, label: str):
    job = _jobs[job_id]
    client = None
    try:
        client = TelegramClient(StringSession(session_str), api_id, api_hash)
        await client.connect()

        if not await client.is_user_authorized():
            raise RuntimeError("Session Telegram sudah tidak valid; hubungkan ulang akun")

        job["log"].append(f"✅ Login sebagai {label}")

        dialogs = await client.get_dialogs()
        groups = []
        for d in dialogs:
            ent = d.entity
            # Supergroup (megagroup=True) atau regular Group
            if isinstance(ent, Channel) and getattr(ent, "megagroup", False):
                groups.append(d)
            elif isinstance(ent, Chat) and not getattr(ent, "deactivated", False):
                groups.append(d)

        job["log"].append(f"📂 {len(groups)} group ditemukan")
        # Mutate the retained set directly. Copying the whole set for every
        # participant made large scrapes increasingly slow and memory-heavy.
        all_contacts: set[str] = job["contacts"]

        for dialog in groups:
            name = dialog.name or "Tanpa Nama"
            job["log"].append(f"🔄 Scraping: {name}...")
            count = 0

            try:
                async for participant in client.iter_participants(dialog.entity):
                    if getattr(participant, "bot", False):
                        continue
                    username = getattr(participant, "username", None)
                    phone = getattr(participant, "phone", None)
                    if username:
                        all_contacts.add(f"@{username}")
                        count += 1
                    elif phone:
                        all_contacts.add(phone)
                        count += 1

                    job["total"] = len(all_contacts)

            except FloodWaitError as e:
                secs = getattr(e, "seconds", 30)
                job["log"].append(f"⏰ Flood wait {secs}s, menunggu...")
                await asyncio.sleep(secs)
            except Exception as e:
                job["log"].append(f"⚠️  Skip [{name}]: {str(e)[:70]}")
                continue

            job["log"].append(f"   └─ ✅ {count} kontak | total unik: {len(all_contacts)}")

        job["status"] = "done"
        job["log"].append(f"🎉 Selesai! {len(all_contacts)} kontak unik terkumpul.")

    except Exception as e:
        job["status"] = "error"
        job["log"].append(f"❌ Error: {str(e)}")
    finally:
        if client and client.is_connected():
            await client.disconnect()


# ─── GET /api/scraper/{job_id}  (polling dari frontend) ─────────────────────
@router.get("/api/scraper/{job_id}")
def scraper_poll(job_id: str, current_user: User = Depends(get_current_user)):
    if job_id not in _jobs or _jobs[job_id].get("user_id") != current_user.id:
        return JSONResponse({"error": "not_found"}, status_code=404)
    job = _jobs[job_id]
    return JSONResponse({
        "status": job["status"],
        "total": job["total"],
        "log": job["log"],
    })


# ─── GET /scraper/download/{job_id}  (download TXT) ─────────────────────────
@router.get("/scraper/download/{job_id}")
def scraper_download(job_id: str, current_user: User = Depends(get_current_user)):
    if job_id not in _jobs or _jobs[job_id].get("user_id") != current_user.id:
        return PlainTextResponse("Job tidak ditemukan", status_code=404)
    job = _jobs[job_id]
    contacts = sorted(job.get("contacts", set()))
    content = "\n".join(contacts)
    return PlainTextResponse(
        content=content,
        headers={
            "Content-Disposition": f"attachment; filename=contacts_{job_id[:8]}.txt",
        },
    )
