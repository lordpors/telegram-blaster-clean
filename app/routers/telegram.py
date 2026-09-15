import json
import math
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import Session
from telethon import TelegramClient
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.sessions import StringSession

from app.auth import get_current_user, verify_csrf
from app.database import DB_PATH, get_db
from app.models import BlastJob, BlastRecipient, TelegramAccount, User
from app.services.blast_manager import blast_manager
from app.services.inbox_manager import inbox_manager
from app.template_utils import jakarta_time

router = APIRouter()
APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.filters["jakarta_time"] = jakarta_time

MIN_DELAY_SECONDS = 0.0
MAX_DELAY_SECONDS = 3600
MAX_RECIPIENTS_PER_JOB = int(os.getenv("MAX_RECIPIENTS_PER_JOB", "500"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
UPLOAD_DIR = Path(
    os.getenv("BLASTER_UPLOAD_DIR", str(DB_PATH.parent / "uploads"))
).expanduser().resolve()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _render(request, template, ctx):
    ctx["request"] = request
    return templates.TemplateResponse(request, template, ctx)


def _back_dashboard():
    return RedirectResponse(url="/dashboard", status_code=303)


def _normalize_username(raw: str) -> tuple[str, str] | None:
    value = raw.strip()
    if not value:
        return None
    value = re.sub(r"^https?://t\.me/", "", value, flags=re.I)
    value = re.sub(r"^t\.me/", "", value, flags=re.I)
    value = value.split("?")[0].split("/")[0].lstrip("@").strip()
    if not value or len(value) > 255 or any(character.isspace() for character in value):
        return None
    return value, value.casefold()


def _normalize_delay_range(delay_min: float, delay_max: float) -> tuple[float, float]:
    """Accept zero delay while keeping malformed and negative values bounded."""
    try:
        normalized_min = float(delay_min)
        normalized_max = float(delay_max)
    except (TypeError, ValueError):
        return MIN_DELAY_SECONDS, MIN_DELAY_SECONDS
    if not math.isfinite(normalized_min) or not math.isfinite(normalized_max):
        return MIN_DELAY_SECONDS, MIN_DELAY_SECONDS
    normalized_min = min(MAX_DELAY_SECONDS, max(MIN_DELAY_SECONDS, normalized_min))
    normalized_max = min(MAX_DELAY_SECONDS, max(normalized_min, normalized_max))
    return normalized_min, normalized_max


# ═══════════════════════════════════════════════════════════════════════════════
# CONNECT FLOW
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/connect-telegram", response_class=HTMLResponse)
async def connect_page(request: Request, _user: User = Depends(get_current_user)):
    return _render(request, "connect_telegram.html", {"step": "connect"})


@router.post("/connect-telegram", response_class=HTMLResponse)
async def connect_submit(
    request: Request,
    phone: str = Form(...),
    api_id: int = Form(...),
    api_hash: str = Form(...),
    label: str = Form(""),
    _user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
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
    _user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
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
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    try:
        client = TelegramClient(StringSession(session_str), api_id, api_hash)
        await client.connect()
        try:
            await client.sign_in(phone=phone, code=otp_code, phone_code_hash=phone_code_hash)
            final_session = client.session.save()
            await client.disconnect()
            return await _save_account(
                request, db, current_user, phone, api_id, api_hash, label, final_session
            )
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
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    try:
        client = TelegramClient(StringSession(session_str), api_id, api_hash)
        await client.connect()
        await client.sign_in(password=password_2fa)
        final_session = client.session.save()
        await client.disconnect()
        return await _save_account(
            request, db, current_user, phone, api_id, api_hash, label, final_session
        )
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


async def _save_account(request, db, current_user, phone, api_id, api_hash, label, session_str):
    """Simpan akun di ruang data pengguna yang sedang login."""
    existing = db.query(TelegramAccount).filter(TelegramAccount.phone == phone).first()
    if existing:
        if existing.user_id != current_user.id:
            return _render(request, "connect_telegram.html", {
                "step": "connect",
                "error_message": "Nomor Telegram ini sudah terhubung ke pengguna lain.",
                "phone": phone,
                "api_id": api_id,
                "api_hash": api_hash,
                "label": label,
            })
        existing.session_str = session_str
        existing.api_id = api_id
        existing.api_hash = api_hash
        existing.is_active = 1
        if label:
            existing.label = label
        account = existing
    else:
        account = TelegramAccount(
            user_id=current_user.id,
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
    inbox_manager.start(account.id)
    blast_manager.refresh_account_pool(current_user.id)

    return _render(request, "connect_telegram.html", {
        "step": "blast",
        "success_message": f"✅ Akun {phone} tersimpan! Akun ini sekarang bisa dipilih per job tanpa mengganggu tab lain.",
        "phone": phone,
        "account_id": account.id,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# ACCOUNT MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/stop-job/{job_id}")
def stop_job(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    """Hentikan blast job yang sedang berjalan secara graceful."""
    job = db.query(BlastJob).filter(
        BlastJob.id == job_id,
        BlastJob.user_id == current_user.id,
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job tidak ditemukan")
    blast_manager.stop_job(job_id)
    active = job.status in ("running", "queued") or (job.source == "sheet" and job.status == "paused")
    if active:
        db.query(BlastRecipient).filter(
            BlastRecipient.job_id == job_id,
            BlastRecipient.status.in_(["pending", "paused"]),
        ).update(
            {BlastRecipient.status: "skipped", BlastRecipient.error: "Dihentikan oleh pengguna"},
            synchronize_session=False,
        )
        job.status = "cancelled"
        job.completed_at = datetime.utcnow()
        blast_manager._refresh_counts(db, job_id)
    return RedirectResponse(url=f"/blast?job_id={job_id}", status_code=303)


@router.post("/delete-account/{account_id}")
async def delete_account(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    account = db.query(TelegramAccount).filter(
        TelegramAccount.id == account_id,
        TelegramAccount.user_id == current_user.id,
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="Akun tidak ditemukan")
    running_manual = (
        db.query(BlastRecipient.id)
        .join(BlastJob, BlastJob.id == BlastRecipient.job_id)
        .filter(
            BlastRecipient.account_id == account_id,
            BlastJob.user_id == current_user.id,
            BlastJob.source != "sheet",
            BlastJob.status.in_(["queued", "running"]),
            BlastRecipient.status.in_(["pending", "sending"]),
        )
        .first()
    )
    if running_manual:
        return RedirectResponse(url="/dashboard?error=account_busy", status_code=303)

    account.is_active = 0
    db.commit()
    blast_manager.refresh_account_pool(current_user.id)
    await inbox_manager.stop(account.id)
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
    current_user: User = Depends(get_current_user),
):
    accounts = db.query(TelegramAccount).filter(
        TelegramAccount.user_id == current_user.id,
        TelegramAccount.is_active == 1,
    ).order_by(TelegramAccount.created_at).all()
    if not accounts:
        return RedirectResponse(url="/dashboard", status_code=303)

    selected_ids = {account_id} if account_id and any(a.id == account_id for a in accounts) else {accounts[0].id}
    job = db.query(BlastJob).filter(
        BlastJob.id == job_id,
        BlastJob.user_id == current_user.id,
    ).first() if job_id else None
    recipients = []
    if job:
        recipients = (
            db.query(BlastRecipient)
            .options(joinedload(BlastRecipient.account))
            .filter(BlastRecipient.job_id == job.id)
            .order_by(BlastRecipient.sort_order)
            .all()
        )
        try:
            selected_ids = set(json.loads(job.accounts_json or "[]"))
        except json.JSONDecodeError:
            pass

    recent_jobs = db.query(BlastJob).filter(
        BlastJob.user_id == current_user.id
    ).order_by(BlastJob.created_at.desc()).limit(10).all()
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
    delay_min: float = Form(0.0),
    delay_max: float = Form(0.0),
    image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    accounts = (
        db.query(TelegramAccount)
        .filter(
            TelegramAccount.id.in_(account_ids),
            TelegramAccount.user_id == current_user.id,
            TelegramAccount.is_active == 1,
        )
        .order_by(TelegramAccount.id)
        .all()
    )
    if not accounts:
        return await _blast_form_error(request, db, current_user, account_ids, usernames, message, "Pilih minimal satu akun Telegram yang valid.")

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
        return await _blast_form_error(request, db, current_user, account_ids, usernames, message, "Daftar username kosong atau tidak valid.")
    if len(cleaned) > MAX_RECIPIENTS_PER_JOB:
        return await _blast_form_error(
            request,
            db,
            current_user,
            account_ids,
            usernames,
            message,
            f"Maksimal {MAX_RECIPIENTS_PER_JOB} username per job agar status dan antrean tetap stabil.",
        )

    message = message.strip()
    if not message:
        return await _blast_form_error(request, db, current_user, account_ids, usernames, message, "Pesan tidak boleh kosong.")

    has_image = bool(image and image.filename)
    message_limit = 1024 if has_image else 4096
    if len(message) > message_limit:
        kind = "caption gambar" if has_image else "pesan"
        return await _blast_form_error(
            request,
            db,
            current_user,
            account_ids,
            usernames,
            message,
            f"Panjang {kind} maksimal {message_limit} karakter.",
        )

    image_path = None
    if has_image:
        suffix = Path(image.filename).suffix.lower() or ".jpg"
        if suffix not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            return await _blast_form_error(request, db, current_user, account_ids, usernames, message, "Format gambar harus JPG, PNG, GIF, atau WebP.")
        user_upload_dir = UPLOAD_DIR / str(current_user.id)
        user_upload_dir.mkdir(parents=True, exist_ok=True)
        upload_path = user_upload_dir / f"{uuid.uuid4().hex}{suffix}"
        uploaded_bytes = 0
        try:
            with upload_path.open("xb") as output:
                while chunk := await image.read(1024 * 1024):
                    uploaded_bytes += len(chunk)
                    if uploaded_bytes > MAX_UPLOAD_BYTES:
                        raise ValueError("upload_too_large")
                    output.write(chunk)
        except ValueError:
            upload_path.unlink(missing_ok=True)
            return await _blast_form_error(
                request,
                db,
                current_user,
                account_ids,
                usernames,
                message,
                f"Ukuran gambar maksimal {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
            )
        except Exception:
            upload_path.unlink(missing_ok=True)
            raise
        finally:
            await image.close()
        image_path = str(upload_path)

    # Lock pembuatan job mencegah dua request pada proses yang sama memasukkan
    # target identik sebagai pending secara bersamaan.
    async with blast_manager.enqueue_lock:
        _delay_min, _delay_max = _normalize_delay_range(delay_min, delay_max)
        job = BlastJob(
            user_id=current_user.id,
            status="queued",
            message=message,
            image_path=image_path,
            delay_seconds=_delay_min,
            delay_max_seconds=_delay_max,
            accounts_json=json.dumps([account.id for account in accounts]),
            consent_confirmed=True,
            total_count=len(cleaned),
            pending_count=len(cleaned),
        )
        db.add(job)
        db.flush()

        active_targets = {
            row[0]
            for row in db.query(BlastRecipient.normalized_username)
            .join(BlastJob, BlastJob.id == BlastRecipient.job_id)
            .filter(
                BlastRecipient.normalized_username.in_(seen),
                BlastJob.user_id == current_user.id,
                BlastRecipient.status.in_(["pending", "sending"]),
                BlastJob.status.in_(["queued", "running"]),
            )
            .all()
        }
        for index, (display, normalized) in enumerate(cleaned):
            account = accounts[index % len(accounts)]
            collision = normalized in active_targets
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


async def _blast_form_error(request, db, current_user, selected_ids, usernames, message, error_message):
    accounts = db.query(TelegramAccount).filter(
        TelegramAccount.user_id == current_user.id,
        TelegramAccount.is_active == 1,
    ).order_by(TelegramAccount.created_at).all()
    recent_jobs = db.query(BlastJob).filter(
        BlastJob.user_id == current_user.id
    ).order_by(BlastJob.created_at.desc()).limit(10).all()
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
def job_status(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = db.query(BlastJob).filter(
        BlastJob.id == job_id,
        BlastJob.user_id == current_user.id,
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job tidak ditemukan")

    recipients = (
        db.query(BlastRecipient)
        .options(joinedload(BlastRecipient.account))
        .filter(BlastRecipient.job_id == job_id)
        .order_by(BlastRecipient.sort_order)
        .all()
    )

    status_labels = {
        "pending": "Proses",
        "sending": "Proses",
        "sent": "Terkirim",
        "failed": "Gagal",
        "skipped": "Dilewati",
        "paused": "Proses",
    }
    job_labels = {
        "queued": "Proses",
        "running": "Proses",
        "completed": "Selesai",
        "partial": "Selesai",
        "failed": "Selesai",
        "cancelled": "Dihentikan",
        "paused": "Proses",
    }

    return JSONResponse({
        "id": job.id,
        "status": job.status,
        "status_label": job_labels.get(job.status, job.status),
        "terminal": job.status in {"completed", "partial", "failed", "cancelled"}
        or (job.status == "paused" and job.source != "sheet"),
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
