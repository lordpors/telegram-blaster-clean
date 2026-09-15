import hashlib
import os
import re
import secrets
import smtplib
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from authlib.integrations.base_client.errors import OAuthError
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import (
    DUMMY_PASSWORD_HASH,
    GOOGLE_AUTH_CONFIGURED,
    csrf_token_for,
    get_current_user,
    hash_password,
    normalize_username,
    oauth,
    password_recovery_configured,
    revoke_current_session,
    send_password_reset_email,
    start_user_session,
    upsert_google_user,
    validate_password,
    validate_username,
    verify_password,
    verify_csrf,
)
from app.database import get_db
from app.models import DeviceSession, User


router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parents[1] / "templates")
)


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/dashboard"


def _auth_context(request: Request, **values) -> dict:
    return {
        "request": request,
        "csrf_token": csrf_token_for(request),
        "google_auth_configured": GOOGLE_AUTH_CONFIGURED,
        **values,
    }


def _render(request: Request, template: str, status_code: int = 200, **values):
    return templates.TemplateResponse(
        request,
        template,
        _auth_context(request, **values),
        status_code=status_code,
    )


def _public_base_url(request: Request) -> str:
    configured = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
    if configured:
        return configured
    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if railway_domain:
        return f"https://{railway_domain}"
    return str(request.base_url).rstrip("/")


@router.get("/login", name="login_page")
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard", status_code=303)
    return _render(
        request,
        "login.html",
        error=request.query_params.get("error"),
        notice=request.query_params.get("notice"),
        next_path=_safe_next(request.query_params.get("next")),
        identity="",
        remember_me=False,
    )


@router.post("/login", name="password_login")
def password_login(
    request: Request,
    identity: str = Form(...),
    password: str = Form(...),
    remember_me: str | None = Form(None),
    next_path: str = Form("/dashboard"),
    _csrf: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
):
    normalized = normalize_username(identity)
    user = db.query(User).filter(
        or_(User.username == normalized, User.email == normalized),
        User.is_active.is_(True),
    ).first()
    password_matches = verify_password(
        password,
        user.password_hash if user and user.password_hash else DUMMY_PASSWORD_HASH,
    )
    if not user or not password_matches:
        return _render(
            request,
            "login.html",
            status_code=400,
            error="invalid_credentials",
            notice=None,
            next_path=_safe_next(next_path),
            identity=identity.strip(),
            remember_me=bool(remember_me),
        )

    user.last_login_at = datetime.utcnow()
    db.commit()
    start_user_session(db, request, user.id, remember=bool(remember_me))
    return RedirectResponse(_safe_next(next_path), status_code=303)


@router.get("/register", name="register_page")
def register_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard", status_code=303)
    return _render(request, "register.html", error=None, values={})


@router.post("/register", name="register_account")
def register_account(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    password_confirmation: str = Form(...),
    _csrf: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
):
    normalized_username = normalize_username(username)
    normalized_email = email.strip().casefold()
    values = {"username": username.strip(), "email": normalized_email}
    error = validate_username(normalized_username)
    if not error and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized_email):
        error = "Masukkan alamat email yang valid."
    if not error:
        error = validate_password(password)
    if not error and password != password_confirmation:
        error = "Konfirmasi password tidak sama."
    if error:
        return _render(request, "register.html", status_code=400, error=error, values=values)

    existing = db.query(User).filter(
        or_(User.username == normalized_username, User.email == normalized_email)
    ).first()
    if existing:
        return _render(
            request,
            "register.html",
            status_code=409,
            error="Username atau email sudah digunakan.",
            values=values,
        )

    user = User(
        google_sub=f"local:{uuid.uuid4().hex}",
        username=normalized_username,
        email=normalized_email,
        password_hash=hash_password(password),
        name=username.strip(),
        role="user",
        is_active=True,
        last_login_at=datetime.utcnow(),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _render(
            request,
            "register.html",
            status_code=409,
            error="Username atau email sudah digunakan.",
            values=values,
        )
    db.refresh(user)
    start_user_session(db, request, user.id)
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/forgot-password", name="forgot_password_page")
def forgot_password_page(request: Request):
    return _render(
        request,
        "forgot_password.html",
        error=None,
        submitted=False,
        recovery_configured=password_recovery_configured(),
    )


@router.post("/forgot-password", name="forgot_password")
def forgot_password(
    request: Request,
    email: str = Form(...),
    _csrf: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
):
    configured = password_recovery_configured()
    if not configured:
        return _render(
            request,
            "forgot_password.html",
            status_code=503,
            error="Layanan pemulihan password belum diaktifkan. Silakan hubungi PorsLabs.",
            submitted=False,
            recovery_configured=False,
        )

    normalized_email = email.strip().casefold()
    user = db.query(User).filter(
        User.email == normalized_email,
        User.password_hash.isnot(None),
        User.is_active.is_(True),
    ).first()
    delivery_error = False
    if user:
        token = secrets.token_urlsafe(32)
        user.password_reset_token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        user.password_reset_expires_at = datetime.utcnow() + timedelta(minutes=30)
        db.commit()
        try:
            send_password_reset_email(
                user.email,
                f"{_public_base_url(request)}/reset-password?token={token}",
            )
        except (OSError, RuntimeError, smtplib.SMTPException):
            delivery_error = True

    return _render(
        request,
        "forgot_password.html",
        status_code=502 if delivery_error else 200,
        error=("Email pemulihan belum dapat dikirim. Silakan coba kembali nanti." if delivery_error else None),
        submitted=not delivery_error,
        recovery_configured=True,
    )


@router.get("/reset-password", name="reset_password_page")
def reset_password_page(request: Request, token: str = "", db: Session = Depends(get_db)):
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""
    user = db.query(User).filter(
        User.password_reset_token_hash == token_hash,
        User.password_reset_expires_at > datetime.utcnow(),
        User.is_active.is_(True),
    ).first()
    return _render(
        request,
        "reset_password.html",
        token=token,
        token_valid=bool(user),
        error=None,
    )


@router.post("/reset-password", name="reset_password")
def reset_password(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    password_confirmation: str = Form(...),
    _csrf: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
):
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    user = db.query(User).filter(
        User.password_reset_token_hash == token_hash,
        User.password_reset_expires_at > datetime.utcnow(),
        User.is_active.is_(True),
    ).first()
    error = validate_password(password)
    if not error and password != password_confirmation:
        error = "Konfirmasi password tidak sama."
    if not user:
        error = "Tautan reset tidak valid atau sudah kedaluwarsa."
    if error:
        return _render(
            request,
            "reset_password.html",
            status_code=400,
            token=token,
            token_valid=bool(user),
            error=error,
        )

    user.password_hash = hash_password(password)
    user.password_reset_token_hash = None
    user.password_reset_expires_at = None
    db.query(DeviceSession).filter(
        DeviceSession.user_id == user.id,
        DeviceSession.revoked_at.is_(None),
    ).update({DeviceSession.revoked_at: datetime.utcnow()}, synchronize_session=False)
    db.commit()
    request.session.clear()
    return RedirectResponse("/login?notice=password_reset", status_code=303)


@router.get("/auth/google/login", name="google_login")
async def google_login(request: Request):
    if not GOOGLE_AUTH_CONFIGURED:
        return RedirectResponse("/login?error=oauth_not_configured", status_code=303)
    request.session["post_login_next"] = _safe_next(request.query_params.get("next"))
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", "").strip() or str(
        request.url_for("google_callback")
    )
    google = oauth.create_client("google")
    return await google.authorize_redirect(request, redirect_uri, prompt="select_account")


@router.get("/auth/google/callback", name="google_callback")
async def google_callback(request: Request, db: Session = Depends(get_db)):
    if not GOOGLE_AUTH_CONFIGURED:
        return RedirectResponse("/login?error=oauth_not_configured", status_code=303)
    try:
        google = oauth.create_client("google")
        token = await google.authorize_access_token(request)
        userinfo = dict(token.get("userinfo") or {})
        user = upsert_google_user(db, userinfo)
    except OAuthError:
        return RedirectResponse("/login?error=google_login_failed", status_code=303)

    destination = _safe_next(request.session.get("post_login_next"))
    start_user_session(db, request, user.id, remember=True)
    return RedirectResponse(destination, status_code=303)


@router.post("/logout")
def logout(
    request: Request,
    _user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
):
    revoke_current_session(db, request)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
