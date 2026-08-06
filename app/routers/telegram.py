import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from telethon import TelegramClient
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.sessions import StringSession

from app.database import get_db
from app.models import BlastJob, BlastRecipient, TelegramAccount
from app.services.blast_manager import blast_manager

router = APIRouter()
APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

SAFE_DELAY_SECONDS = 5
MAX_RECIPIENTS_PER_JOB = int(os.getenv("MAX_RECIPIENTS_PER_JOB", "200"))
UPLOAD_DIR = APP_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _render(request, template, ctx):
    ctx["request"] = request
    return templates.TemplateResponse(template, ctx)


def _back_dashboard():
    return RedirectResponse(url="/dashboard", status_code=303)


def _normalize_username(raw: str) -> tuple[str, str] | None:
    value = raw.strip()
    if not value:
        return None
    value = re.sub(r"^https?://t\.me/", "", value, flags=re.I)
    value = re.sub(r"^t\.me/", "", value, flags=re.I)
    value = value.split("?")[0].split("/")[0].lstrip("@").strip()
    if not value:
        return None
    return value, value.casefold()


# ═══════════════════════════════════════════════════════════════════════════════
# CONNECT FLOW
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/connect-telegram", response_class=HTMLResponse)
async def connect_page(request: Request):
    return _render(request, "connect_telegram.html", {"step": "connect"})


@router.post("/connect-telegram", response_class=HTMLResponse)
async def connect_submit(
    request: Request,
    phone: str = Form(...),
    api_id: int = Form(...),
    api_hash: str = Form(...),
    label: str = Form(""),
):
    try:
        client = TelegramClient(StringSession(), api_id, api_hash)
        await client.connect()
        sent = await client.send_code_request(phone)
        session_str = client.session.save()
        phone_code_hash = sent.phone_code_hash
        await client.disconnect()

        return _render(request, "connect_telegram.html", {
            "step": "otp",
            "success_message": f"Kode OTP dikirim ke {phone}.",
            "phone": phone,
            "api_id": api_id,
            "api_hash": api_hash,
            "label": label,
            "session_str": session_str,
            "phone_code_hash": phone_code_hash,
        })
    except FloodWaitError as exc:
        return _render(request, "connect_telegram.html", {
            "step": "connect",
            "error_message": f"⏳ Flood limit. Tunggu {exc.seconds} detik.",
            "phone": phone,
            "api_id": api_id,
            "api_hash": api_hash,
            "label": label,
        })
    except Exception as exc:
        return _render(request, "connect_telegram.html", {
            "step": "connect",
            "error_message": f"Gagal kirim OTP: {exc}",
            "phone": phone,
            "api_id": api_id,
            "api_hash": api_hash,
            "label": label,
        })


@router.post("/resend-otp", response_class=HTMLResponse)
async def resend_otp(
    request: Request,
    phone: str = Form(...),
    api_id: int = Form(...),
    api_hash: str = Form(...),
    label: str = Form(""),
):
    try:
        client = TelegramClient(StringSession(), api_id, api_hash)
        await client.connect()
        sent = await client.send_code_request(phone)
        session_str = client.session.save()
        phone_code_hash = sent.phone_code_hash
        await client.disconnect()

        return _render(request, "connect_telegram.html", {
            "step": "otp",
            "success_message": f"✅ OTP baru dikirim ke {phone}.",
            "phone": phone,
            "api_id": api_id,
            "api_hash": api_hash,
            "label": label,
            "session_str": session_str,
            "phone_code_hash": phone_code_hash,
        })
    except FloodWaitError as exc:
        return _render(request, "connect_telegram.html", {
            "step": "otp",
            "error_message": f"⏳ Flood limit. Tunggu {exc.seconds} detik.",
            "phone": phone,
            "api_id": api_id,
            "api_hash": api_hash,
            "label": label,
        })
    except Exception as exc:
        return _render(request, "connect_telegram.html", {
            "step": "otp",
            "error_message": f"Gagal: {exc}",
            "phone": phone,
            "api_id": api_id,
            "api_hash": api_hash,
            "label": label,
        })


@router.post("/verify-otp", response_class=HTMLResponse)
async def verify_otp(
    request: Request,
    phone: str = Form(...),
    api_id: int = Form(...),
    api_hash: str = Form(...),
    label: str = Form(""),
    session_str: str = Form(...),
    phone_code_hash: str = Form(...),
    otp_code: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        client = TelegramClient(StringSession(session_str), api_id, api_hash)
        await client.connect()
        try:
            await client.sign_in(phone=phone, code=otp_code, phone_code_hash=phone_code_hash)
            final_session = client.session.save()
            await client.disconnect()
            return await _save_account(request, db, phone, api_id, api_hash, label, final_session)
        except SessionPasswordNeededError:
            saved_session = client.session.save()
            await client.disconnect()
            return _render(request, "connect_telegram.html", {
                "step": "2fa",
                "success_message": "OTP benar! Masukkan password 2FA Telegram.",
                "phone": phone,
                "api_id": api_id,
                "api_hash": api_hash,
                "label": label,
                "session_str": saved_session,
            })
    except FloodWaitError as exc:
        return _render(request, "connect_telegram.html", {
            "step": "otp",
            "error_message": f"⏳ Flood limit. Tunggu {exc.seconds} detik.",
            "phone": phone,
            "api_id": api_id,
            "api_hash": api_hash,
            "label": label,
            "session_str": session_str,
            "phone_code_hash": phone_code_hash,
        })
    except Exception as exc:
        return _render(request, "connect_telegram.html", {
            "step": "otp",
            "error_message": f"OTP salah atau expired: {exc}",
            "phone": phone,
            "api_id": api_id,
            "api_hash": api_hash,
            "label": label,
            "session_str": session_str,
            "phone_code_hash": phone_code_hash,
        })


@router.post("/verify-2fa", response_class=HTMLResponse)
async def verify_2fa(
    request: Request,
    phone: str = Form(...),
    api_id: int = Form(...),
    api_hash: str = Form(...),
    label: str = Form(""),
    session_str: str = Form(...),
    password_2fa: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        client = TelegramClient(StringSession(session_str), api_id, api_hash)
        await client.connect()
        await client.sign_in(password=password_2fa)
        final_session = client.session.save()
        await client.disconnect()
        return await _save_account(request, db, phone, api_id, api_hash, label, final_session)
    except FloodWaitError as exc:
        return _render(request, "connect_telegram.html", {
            "step": "2fa",
            "error_message": f"⏳ Flood limit. Tunggu {exc.seconds} detik.",
            "phone": phone,
            "api_id": api_id,
            "api_hash": api_hash,
            "label": label,
            "session_str": session_str,
        })
    except Exception as exc:
        return _render(request, "connect_telegram.html", {
            "step": "2fa",
            "error_message": f"Password salah: {exc}",
            "phone": phone,
            "api_id": api_id,
            "api_hash": api_hash,
            "label": label,
            "session_str": session_str,
        })


async def _save_account(request, db, phone, api_id, api_hash, label, session_str):
    """Simpan akun tanpa mengganti akun global milik tab lain."""
    existing = db.query(TelegramAccount).filter(TelegramAccount.phone == phone).first()
    if existing:
        existing.session_str = session_str
        existing.api_id = api_id
        existing.api_hash = api_hash
        existing.is_active = 1
        if label:
            existing.label = label
        account = existing
    else:
        account = TelegramAccount(
            phone=phone,
            api_id=api_id,
            api_hash=api_hash,
            label=label or phone,
            session_str=session_str,
            is_active=1,
        )
        db.add(account)
    db.commit()
    db.refresh(account)

    return _render(request, "connect_telegram.html", {
        "step": "blast",
        "success_message": f"✅ Akun {phone} tersimpan! Akun ini sekarang bisa dipilih per job tanpa mengganggu tab lain.",
        "phone": phone,
        "account_id": account.id,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# ACCOUNT MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/switch-account/{account_id}")
def switch_account(account_id: int, db: Session = Depends(get_db)):
    """Kompatibilitas lama: akun hanya ditandai terhubung, tidak mematikan akun lain."""
    account = db.get(TelegramAccount, account_id)
    if account:
        account.is_active = 1
        db.commit()
    return RedirectResponse(url=f"/blast?account_id={account_id}", status_code=303)


@router.post("/stop-job/{job_id}")
def stop_job(job_id: int, db: Session = Depends(get_db)):
    """Hentikan blast job yang sedang berjalan secara graceful."""
    blast_manager.stop_job(job_id)
    # Pastikan recipient yang masih pending di-pause di DB
    job = db.get(BlastJob, job_id)
    if job and job.status in ("running", "queued"):
        db.query(BlastRecipient).filter(
            BlastRecipient.job_id == job_id,
            BlastRecipient.status.in_(["pending"]),
        ).update(
            {BlastRecipient.status: "paused", BlastRecipient.error: "Dihentikan oleh pengguna"},
            synchronize_session=False,
        )
        job.status = "paused"
        job.completed_at = datetime.utcnow()
        db.commit()
    return RedirectResponse(url=f"/blast?job_id={job_id}", status_code=303)


@router.post("/delete-account/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    running = (
        db.query(BlastRecipient.id)
        .join(BlastJob, BlastJob.id == BlastRecipient.job_id)
        .filter(
            BlastRecipient.account_id == account_id,
            BlastJob.status.in_(["queued", "running"]),
            BlastRecipient.status.in_(["pending", "sending"]),
        )
        .first()
    )
    if running:
        return RedirectResponse(url="/dashboard?error=account_busy", status_code=303)

    account = db.get(TelegramAccount, account_id)
    if account:
        db.delete(account)
        db.commit()
    return _back_dashboard()


# ═══════════════════════════════════════════════════════════════════════════════
# BLAST JOBS
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/blast", response_class=HTMLResponse)
async def blast_page(
    request: Request,
    account_id: int | None = None,
    job_id: int | None = None,
    db: Session = Depends(get_db),
):
    accounts = db.query(TelegramAccount).filter(TelegramAccount.is_active == 1).order_by(TelegramAccount.created_at).all()
    if not accounts:
        return RedirectResponse(url="/dashboard", status_code=303)

    selected_ids = {account_id} if account_id and any(a.id == account_id for a in accounts) else {accounts[0].id}
    job = db.get(BlastJob, job_id) if job_id else None
    recipients = []
    if job:
        recipients = (
            db.query(BlastRecipient)
            .filter(BlastRecipient.job_id == job.id)
            .order_by(BlastRecipient.sort_order)
            .all()
        )
        try:
            selected_ids = set(json.loads(job.accounts_json or "[]"))
        except json.JSONDecodeError:
            pass

    recent_jobs = db.query(BlastJob).order_by(BlastJob.created_at.desc()).limit(10).all()
    return _render(request, "blast.html", {
        "accounts": accounts,
        "selected_ids": selected_ids,
        "job": job,
        "recipients": recipients,
        "recent_jobs": recent_jobs,
        "max_recipients": MAX_RECIPIENTS_PER_JOB,
    })


@router.post("/send-blast")
async def send_blast(
    request: Request,
    account_ids: List[int] = Form(...),
    usernames: str = Form(...),
    message: str = Form(...),
    consent_confirmed: bool = Form(False),
    delay_min: float = Form(5.0),
    delay_max: float = Form(5.0),
    image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    if not consent_confirmed:
        return await _blast_form_error(request, db, account_ids, usernames, message, "Konfirmasi penerima opt-in wajib dicentang.")

    accounts = (
        db.query(TelegramAccount)
        .filter(TelegramAccount.id.in_(account_ids), TelegramAccount.is_active == 1)
        .order_by(TelegramAccount.id)
        .all()
    )
    if not accounts:
        return await _blast_form_error(request, db, account_ids, usernames, message, "Pilih minimal satu akun Telegram yang valid.")

    cleaned = []
    seen = set()
    for raw in usernames.splitlines():
        normalized = _normalize_username(raw)
        if not normalized:
            continue
        display, key = normalized
        if key in seen:
            continue
        seen.add(key)
        cleaned.append((display, key))

    if not cleaned:
        return await _blast_form_error(request, db, account_ids, usernames, message, "Daftar username kosong atau tidak valid.")
    if len(cleaned) > MAX_RECIPIENTS_PER_JOB:
        return await _blast_form_error(
            request,
            db,
            account_ids,
            usernames,
            message,
            f"Maksimal {MAX_RECIPIENTS_PER_JOB} username per job agar status dan antrean tetap stabil.",
        )

    image_path = None
    if image and image.filename:
        suffix = Path(image.filename).suffix.lower() or ".jpg"
        if suffix not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            return await _blast_form_error(request, db, account_ids, usernames, message, "Format gambar harus JPG, PNG, GIF, atau WebP.")
        image_path = str(UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}")
        content = await image.read()
        if len(content) > 10 * 1024 * 1024:
            return await _blast_form_error(request, db, account_ids, usernames, message, "Ukuran gambar maksimal 10 MB.")
        Path(image_path).write_bytes(content)

    # Lock pembuatan job mencegah dua request pada proses yang sama memasukkan
    # target identik sebagai pending secara bersamaan.
    async with blast_manager.enqueue_lock:
        _delay_min = max(0.5, float(delay_min or 5))
        _delay_max = max(_delay_min, float(delay_max or _delay_min))
        job = BlastJob(
            status="queued",
            message=message,
            image_path=image_path,
            delay_seconds=int(_delay_min),
            delay_max_seconds=_delay_max,
            accounts_json=json.dumps([account.id for account in accounts]),
            consent_confirmed=True,
            total_count=len(cleaned),
            pending_count=len(cleaned),
        )
        db.add(job)
        db.flush()

        for index, (display, normalized) in enumerate(cleaned):
            account = accounts[index % len(accounts)]
            collision = (
                db.query(BlastRecipient.id)
                .join(BlastJob, BlastJob.id == BlastRecipient.job_id)
                .filter(
                    BlastRecipient.normalized_username == normalized,
                    BlastRecipient.status.in_(["pending", "sending"]),
                    BlastJob.status.in_(["queued", "running"]),
                )
                .first()
            )
            status = "skipped" if collision else "pending"
            error = "Dilewati: username sedang berada di antrean job lain" if collision else None
            db.add(BlastRecipient(
                job_id=job.id,
                account_id=account.id,
                username=display,
                normalized_username=normalized,
                sort_order=index,
                status=status,
                error=error,
            ))

        db.commit()
        blast_manager._refresh_counts(db, job.id)
        db.refresh(job)

    blast_manager.start_job(job.id)
    return RedirectResponse(url=f"/blast?job_id={job.id}", status_code=303)


async def _blast_form_error(request, db, selected_ids, usernames, message, error_message):
    accounts = db.query(TelegramAccount).filter(TelegramAccount.is_active == 1).order_by(TelegramAccount.created_at).all()
    recent_jobs = db.query(BlastJob).order_by(BlastJob.created_at.desc()).limit(10).all()
    return _render(request, "blast.html", {
        "accounts": accounts,
        "selected_ids": set(selected_ids),
        "job": None,
        "recipients": [],
        "recent_jobs": recent_jobs,
        "prev_usernames": usernames,
        "prev_message": message,
        "error_message": error_message,
        "max_recipients": MAX_RECIPIENTS_PER_JOB,
    })


@router.get("/api/jobs/{job_id}")
def job_status(job_id: int, db: Session = Depends(get_db)):
    job = db.get(BlastJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job tidak ditemukan")

    recipients = (
        db.query(BlastRecipient)
        .filter(BlastRecipient.job_id == job_id)
        .order_by(BlastRecipient.sort_order)
        .all()
    )

    status_labels = {
        "pending": "Belum dikirim",
        "sending": "Sedang mengirim",
        "sent": "Terkirim",
        "failed": "Gagal",
        "skipped": "Dilewati",
        "paused": "Dijeda — belum dikirim",
    }
    job_labels = {
        "queued": "Menunggu antrean",
        "running": "Sedang berjalan",
        "completed": "Selesai",
        "partial": "Selesai sebagian",
        "failed": "Gagal",
        "paused": "Dijeda oleh Telegram",
    }

    return JSONResponse({
        "id": job.id,
        "status": job.status,
        "status_label": job_labels.get(job.status, job.status),
        "total": job.total_count,
        "sent": job.sent_count,
        "failed": job.failed_count,
        "skipped": job.skipped_count,
        "pending": job.pending_count,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "recipients": [
            {
                "id": row.id,
                "username": row.username,
                "status": row.status,
                "status_label": status_labels.get(row.status, row.status),
                "error": row.error,
                "account": (row.account.label or row.account.phone) if row.account else "Akun dihapus",
            }
            for row in recipients
        ],
    })
