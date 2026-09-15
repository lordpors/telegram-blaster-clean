import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator


ENCRYPTED_PREFIX = "enc:v1:"


def _fernet_from_secret(secret: str) -> Fernet:
    derived = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def _keyring() -> MultiFernet | None:
    secrets = [os.getenv("DATA_ENCRYPTION_KEY", "").strip()]
    secrets.extend(
        item.strip()
        for item in os.getenv("DATA_ENCRYPTION_KEY_OLD", "").split(",")
        if item.strip()
    )
    fernets = [_fernet_from_secret(item) for item in secrets if item]
    return MultiFernet(fernets) if fernets else None


def _current_fernet() -> Fernet | None:
    secret = os.getenv("DATA_ENCRYPTION_KEY", "").strip()
    return _fernet_from_secret(secret) if secret else None


def encryption_configured() -> bool:
    return _current_fernet() is not None


def encrypt_sensitive(value: str | None) -> str | None:
    if value is None or value == "":
        return value
    keyring = _keyring()
    current = _current_fernet()
    if keyring is None or current is None:
        raise RuntimeError("DATA_ENCRYPTION_KEY wajib diatur sebelum menyimpan data Telegram")
    if value.startswith(ENCRYPTED_PREFIX):
        encoded = value[len(ENCRYPTED_PREFIX):].encode("ascii")
        try:
            current.decrypt(encoded)
            return value
        except InvalidToken:
            pass
        plaintext = keyring.decrypt(encoded).decode("utf-8")
    else:
        plaintext = value
    token = current.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{ENCRYPTED_PREFIX}{token}"


def decrypt_sensitive(value: str | None) -> str | None:
    if value is None or value == "" or not value.startswith(ENCRYPTED_PREFIX):
        # Dukungan sementara untuk baris lama selama migrasi satu kali.
        return value
    keyring = _keyring()
    if keyring is None:
        raise RuntimeError("DATA_ENCRYPTION_KEY tidak tersedia untuk membuka data Telegram")
    try:
        return keyring.decrypt(value[len(ENCRYPTED_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Data Telegram tidak dapat didekripsi dengan keyring aktif") from exc


class EncryptedText(TypeDecorator):
    """Encrypt on every write and decrypt transparently on every read."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, _dialect):
        return encrypt_sensitive(value)

    def process_result_value(self, value, _dialect):
        return decrypt_sensitive(value)
