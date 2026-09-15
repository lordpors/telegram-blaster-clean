import hashlib
import os
import re
import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage
from datetime import datetime, timedelta

from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, Form, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DeviceSession, User


GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()
GOOGLE_ALLOWED_EMAILS = {
    item.strip().casefold()
    for item in os.getenv("GOOGLE_ALLOWED_EMAILS", "").split(",")
    if item.strip()
}

GOOGLE_AUTH_CONFIGURED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and SESSION_SECRET)
USERNAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{1,30}[a-z0-9])?$")
PASSWORD_SCRYPT_N = 2**14
PASSWORD_SCRYPT_R = 8
PASSWORD_SCRYPT_P = 1
PASSWORD_SCRYPT_LENGTH = 32
SESSION_SHORT_SECONDS = 60 * 60 * 12
SESSION_REMEMBERED_SECONDS = 60 * 60 * 24 * 30

oauth = OAuth()
if GOOGLE_AUTH_CONFIGURED:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


class AuthenticationRequired(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Silakan masuk untuk melanjutkan.",
        )


def session_secret_for_middleware() -> str:
    # The random fallback is local-only and intentionally invalidates cookies on restart.
    return SESSION_SECRET or secrets.token_urlsafe(32)


def csrf_token_for(request: Request) -> str:
    return request.session.setdefault("csrf_token", secrets.token_urlsafe(32))


def _device_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _device_name(user_agent: str) -> str:
    lowered = user_agent.casefold()
    browser = next(
        (name for marker, name in (
            ("edg/", "Microsoft Edge"),
            ("opr/", "Opera"),
            ("firefox/", "Firefox"),
            ("chrome/", "Chrome"),
            ("safari/", "Safari"),
        ) if marker in lowered),
        "Browser",
    )
    system = next(
        (name for marker, name in (
            ("android", "Android"),
            ("iphone", "iPhone"),
            ("ipad", "iPad"),
            ("windows", "Windows"),
            ("mac os", "macOS"),
            ("linux", "Linux"),
        ) if marker in lowered),
        "Perangkat tidak dikenal",
    )
    return f"{browser} · {system}"


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    value = forwarded or (request.client.host if request.client else "")
    return value[:64] or None


def start_user_session(
    db: Session,
    request: Request,
    user_id: int,
    remember: bool = False,
) -> DeviceSession:
    now = datetime.utcnow()
    lifetime = SESSION_REMEMBERED_SECONDS if remember else SESSION_SHORT_SECONDS
    expires_at = now + timedelta(seconds=lifetime)
    token = secrets.token_urlsafe(32)
    user_agent = request.headers.get("user-agent", "")[:500]
    device = DeviceSession(
        user_id=user_id,
        token_hash=_device_token_hash(token),
        device_name=_device_name(user_agent),
        user_agent=user_agent or None,
        ip_address=_client_ip(request),
        created_at=now,
        last_seen_at=now,
        expires_at=expires_at,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    request.session.clear()
    request.session.update({
        "user_id": user_id,
        "device_token": token,
        "csrf_token": secrets.token_urlsafe(32),
        "expires_at": int(expires_at.timestamp()),
    })
    request.state.device_session = device
    return device


def revoke_current_session(db: Session, request: Request) -> None:
    token = request.session.get("device_token", "")
    if token:
        device = db.query(DeviceSession).filter(
            DeviceSession.token_hash == _device_token_hash(token),
            DeviceSession.revoked_at.is_(None),
        ).first()
        if device:
            device.revoked_at = datetime.utcnow()
            db.commit()


def normalize_username(value: str) -> str:
    return value.strip().casefold()


def validate_username(value: str) -> str | None:
    username = normalize_username(value)
    if not USERNAME_PATTERN.fullmatch(username):
        return "Username harus 3–32 karakter dan hanya memakai huruf, angka, titik, garis bawah, atau tanda minus."
    return None


def validate_password(value: str) -> str | None:
    if len(value) < 8:
        return "Password minimal 8 karakter."
    if len(value) > 128:
        return "Password maksimal 128 karakter."
    return None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=PASSWORD_SCRYPT_N,
        r=PASSWORD_SCRYPT_R,
        p=PASSWORD_SCRYPT_P,
        dklen=PASSWORD_SCRYPT_LENGTH,
    )
    return "$".join((
        "scrypt",
        str(PASSWORD_SCRYPT_N),
        str(PASSWORD_SCRYPT_R),
        str(PASSWORD_SCRYPT_P),
        salt.hex(),
        derived.hex(),
    ))


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        algorithm, n, r, p, salt_hex, expected_hex = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        expected = bytes.fromhex(expected_hex)
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(derived, expected)


# Menyamakan biaya komputasi login untuk username yang ada dan tidak ada.
DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


def password_recovery_configured() -> bool:
    return bool(os.getenv("SMTP_HOST", "").strip() and os.getenv("SMTP_FROM_EMAIL", "").strip())


def send_password_reset_email(recipient: str, reset_url: str) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    sender = os.getenv("SMTP_FROM_EMAIL", "").strip()
    if not host or not sender:
        raise RuntimeError("SMTP belum dikonfigurasi")

    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    use_tls = os.getenv("SMTP_USE_TLS", "true").strip().casefold() in {"1", "true", "yes"}
    message = EmailMessage()
    message["Subject"] = "Atur ulang password PorsLabs Telegram Blaster"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(
        "Kami menerima permintaan untuk mengatur ulang password akun Anda.\n\n"
        f"Buka tautan ini dalam 30 menit:\n{reset_url}\n\n"
        "Jika Anda tidak meminta perubahan ini, abaikan email ini."
    )

    with smtplib.SMTP(host, port, timeout=12) as smtp:
        if use_tls:
            smtp.starttls(context=ssl.create_default_context())
        if username:
            smtp.login(username, password)
        smtp.send_message(message)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = None
    user_id = request.session.get("user_id")
    expires_at = request.session.get("expires_at")
    if user_id and (not isinstance(expires_at, (int, float)) or expires_at <= time.time()):
        request.session.clear()
        user_id = None
    if user_id:
        user = db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()
        if not user:
            request.session.clear()

    if not user:
        raise AuthenticationRequired()

    token = request.session.get("device_token", "")
    device = None
    if token:
        device = db.query(DeviceSession).filter(
            DeviceSession.token_hash == _device_token_hash(token),
            DeviceSession.user_id == user.id,
            DeviceSession.revoked_at.is_(None),
            DeviceSession.expires_at > datetime.utcnow(),
        ).first()
        if not device:
            request.session.clear()
            raise AuthenticationRequired()
    else:
        # Daftarkan cookie valid dari deployment sebelum manajemen perangkat tersedia.
        device = start_user_session(
            db,
            request,
            user.id,
            remember=(expires_at - time.time()) > SESSION_SHORT_SECONDS,
        )

    if device.last_seen_at < datetime.utcnow() - timedelta(minutes=5):
        device.last_seen_at = datetime.utcnow()
        device.ip_address = _client_ip(request)
        db.commit()

    request.state.current_user = user
    request.state.device_session = device
    csrf_token_for(request)
    return user


def verify_csrf(request: Request, csrf_token: str = Form(...)) -> None:
    expected = request.session.get("csrf_token", "")
    if not expected or not secrets.compare_digest(expected, csrf_token):
        raise HTTPException(status_code=403, detail="Form keamanan kedaluwarsa. Muat ulang halaman.")


def upsert_google_user(db: Session, userinfo: dict) -> User:
    google_sub = str(userinfo.get("sub", "")).strip()
    email = str(userinfo.get("email", "")).strip().casefold()
    if not google_sub or not email or not userinfo.get("email_verified"):
        raise HTTPException(status_code=403, detail="Akun Google tidak memiliki email terverifikasi.")
    if GOOGLE_ALLOWED_EMAILS and email not in GOOGLE_ALLOWED_EMAILS:
        raise HTTPException(status_code=403, detail="Email ini belum diizinkan menggunakan aplikasi.")

    user = db.query(User).filter(User.google_sub == google_sub).first()
    if not user:
        bootstrap_user = db.query(User).filter(User.email == email).first()
        if bootstrap_user and bootstrap_user.google_sub.startswith(("bootstrap:", "local:")):
            user = bootstrap_user
            user.google_sub = google_sub
        elif bootstrap_user:
            raise HTTPException(status_code=409, detail="Email sudah terhubung ke identitas lain.")
        else:
            user = User(
                google_sub=google_sub,
                email=email,
                role="user",
                is_active=True,
            )
            db.add(user)

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Akun pengguna sedang dinonaktifkan.")

    user.email = email
    user.name = str(userinfo.get("name") or email.split("@", 1)[0])[:255]
    user.picture_url = str(userinfo.get("picture") or "") or None
    user.last_login_at = datetime.utcnow()
    db.commit()
    db.refresh(user)
    return user
