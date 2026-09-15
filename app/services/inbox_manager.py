import asyncio
import logging
import mimetypes
import os
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from telethon import TelegramClient, events, functions, types
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)
from telethon.sessions import StringSession

from app.database import DB_PATH, SessionLocal
from app.models import InboxConversation, InboxMessage, TelegramAccount, User


logger = logging.getLogger(__name__)
INBOX_MEDIA_DIR = Path(
    os.getenv("INBOX_MEDIA_DIR", str(DB_PATH.parent / "inbox-media"))
).expanduser().resolve()
MAX_INBOX_MEDIA_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
PERMANENT_SESSION_ERRORS = (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)
AUTO_REPLY_COOLDOWN = timedelta(hours=24)


class InboxManager:
    def __init__(self):
        self.tasks: dict[int, asyncio.Task] = {}
        self.clients: dict[int, TelegramClient] = {}

    @staticmethod
    def enabled() -> bool:
        return os.getenv("INBOX_LISTENERS_ENABLED", "true").strip().casefold() in {
            "1",
            "true",
            "yes",
        }

    @staticmethod
    def account_enabled(account_id: int) -> bool:
        if os.getenv("RAILWAY_ENVIRONMENT", "").strip():
            return True
        selected = os.getenv("INBOX_ACCOUNT_ID", "").strip()
        return not selected or selected == str(account_id)

    def connected(self, account_id: int) -> bool:
        task = self.tasks.get(account_id)
        client = self.clients.get(account_id)
        return bool(task and not task.done() and client and client.is_connected())

    @staticmethod
    def _set_account_active(account_id: int, active: bool) -> None:
        with SessionLocal() as db:
            account = db.get(TelegramAccount, account_id)
            if account and bool(account.is_active) != active:
                account.is_active = int(active)
                db.commit()

    async def start_all(self) -> None:
        if not self.enabled():
            return
        with SessionLocal() as db:
            account_ids = [
                row[0]
                for row in db.query(TelegramAccount.id)
                .filter(TelegramAccount.session_str.isnot(None))
                .all()
                if self.account_enabled(row[0])
            ]
        for index, account_id in enumerate(account_ids, start=1):
            self.start(account_id)
            if index % 10 == 0:
                await asyncio.sleep(1)

    def start(self, account_id: int) -> None:
        if (
            not self.enabled()
            or not self.account_enabled(account_id)
            or (account_id in self.tasks and not self.tasks[account_id].done())
        ):
            return
        task = asyncio.create_task(self._listen(account_id), name=f"inbox-account-{account_id}")
        self.tasks[account_id] = task
        task.add_done_callback(
            lambda done, aid=account_id: self.tasks.pop(aid, None)
            if self.tasks.get(aid) is done
            else None
        )

    async def stop(self, account_id: int) -> None:
        task = self.tasks.pop(account_id, None)
        if task:
            task.cancel()
        client = self.clients.pop(account_id, None)
        if client and client.is_connected():
            await client.disconnect()
        if task:
            await asyncio.gather(task, return_exceptions=True)

    async def shutdown(self) -> None:
        await asyncio.gather(*(self.stop(account_id) for account_id in list(self.tasks)))

    async def _listen(self, account_id: int) -> None:
        while True:
            with SessionLocal() as db:
                account = db.get(TelegramAccount, account_id)
                if not account or not account.session_str:
                    return
                credentials = (account.session_str, account.api_id, account.api_hash)

            client = TelegramClient(StringSession(credentials[0]), credentials[1], credentials[2])

            async def on_message(event):
                await self._store_incoming(account_id, event)

            client.add_event_handler(on_message, events.NewMessage(incoming=True))
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    logger.warning("Inbox listener account %s is no longer authorized", account_id)
                    self._set_account_active(account_id, False)
                    return
                self._set_account_active(account_id, True)
                self.clients[account_id] = client
                await client.run_until_disconnected()
            except asyncio.CancelledError:
                raise
            except PERMANENT_SESSION_ERRORS as exc:
                self._set_account_active(account_id, False)
                logger.error(
                    "Inbox listener account %s stopped until reconnected: %s",
                    account_id,
                    type(exc).__name__,
                )
                return
            except Exception:
                logger.exception("Inbox listener account %s disconnected", account_id)
            finally:
                if self.clients.get(account_id) is client:
                    self.clients.pop(account_id, None)
                if client.is_connected():
                    await client.disconnect()
            await asyncio.sleep(5)

    async def _store_incoming(self, account_id: int, event) -> None:
        if not event.is_private:
            return
        input_peer = await event.get_input_chat()
        if not isinstance(input_peer, types.InputPeerUser):
            return
        peer = await event.get_chat()
        body = (event.raw_text or "").strip()
        if not body:
            if not event.message.media:
                return
            body = "📎 Media"
        peer_name = " ".join(
            part for part in (getattr(peer, "first_name", None), getattr(peer, "last_name", None)) if part
        ) or getattr(peer, "username", None) or str(input_peer.user_id)
        created_at = event.message.date or datetime.utcnow()
        if created_at.tzinfo:
            created_at = created_at.replace(tzinfo=None)

        media_path = None
        media_name = None
        media_type = None
        avatar_path = None
        event_client = getattr(event, "client", None)
        media_dir = INBOX_MEDIA_DIR / str(account_id) / str(input_peer.user_id)
        if event_client:
            media_dir.mkdir(parents=True, exist_ok=True)
            avatar_target = media_dir / "avatar.jpg"
            if not avatar_target.exists():
                try:
                    downloaded = await event_client.download_profile_photo(
                        peer, file=str(avatar_target)
                    )
                    avatar_path = str(downloaded) if downloaded else None
                except Exception:
                    logger.debug("Profile photo unavailable for peer %s", input_peer.user_id)
            else:
                avatar_path = str(avatar_target)

            file_size = getattr(getattr(event.message, "file", None), "size", 0) or 0
            if event.message.media and file_size <= MAX_INBOX_MEDIA_BYTES:
                try:
                    downloaded = await event.download_media(file=str(media_dir))
                    if downloaded:
                        media_path = str(Path(downloaded).resolve())
                        media_name = Path(downloaded).name[:255]
                        media_type = mimetypes.guess_type(media_name)[0] or "application/octet-stream"
                except Exception:
                    logger.exception("Failed to cache inbox media for peer %s", input_peer.user_id)

        with SessionLocal() as db:
            account = db.get(TelegramAccount, account_id)
            if not account:
                return
            conversation = db.query(InboxConversation).filter(
                InboxConversation.account_id == account.id,
                InboxConversation.peer_id == input_peer.user_id,
            ).first()
            if not conversation:
                conversation = InboxConversation(
                    user_id=account.user_id,
                    account_id=account.id,
                    peer_id=input_peer.user_id,
                    peer_access_hash=input_peer.access_hash,
                    peer_name=peer_name[:255],
                    peer_username=(getattr(peer, "username", None) or None),
                    created_at=created_at,
                )
                db.add(conversation)
            conversation.peer_access_hash = input_peer.access_hash
            conversation.peer_name = peer_name[:255]
            conversation.peer_username = getattr(peer, "username", None) or None
            conversation.avatar_path = avatar_path or conversation.avatar_path
            conversation.marked_unread = False
            conversation.updated_at = created_at
            db.add(InboxMessage(
                user_id=account.user_id,
                account_id=account.id,
                peer_id=input_peer.user_id,
                peer_access_hash=input_peer.access_hash,
                peer_name=peer_name[:255],
                peer_username=(getattr(peer, "username", None) or None),
                telegram_message_id=event.message.id,
                direction="in",
                body=body,
                is_read=False,
                media_path=media_path,
                media_name=media_name,
                media_type=media_type,
                created_at=created_at,
            ))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return

        claimed = self._claim_auto_reply(account_id, input_peer.user_id)
        if not claimed:
            return
        auto_reply, claimed_at = claimed
        try:
            telegram_message_id, sent_at = await self.send_reply(
                account_id,
                input_peer.user_id,
                input_peer.access_hash,
                auto_reply,
            )
        except Exception:
            logger.exception("Auto-reply failed for account %s", account_id)
            with SessionLocal() as db:
                db.query(InboxConversation).filter(
                    InboxConversation.account_id == account_id,
                    InboxConversation.peer_id == input_peer.user_id,
                    InboxConversation.auto_replied_at == claimed_at,
                ).update({InboxConversation.auto_replied_at: None})
                db.commit()
            return

        with SessionLocal() as db:
            account = db.get(TelegramAccount, account_id)
            conversation = db.query(InboxConversation).filter(
                InboxConversation.account_id == account_id,
                InboxConversation.peer_id == input_peer.user_id,
            ).first()
            if not account or not conversation:
                return
            db.add(InboxMessage(
                user_id=account.user_id,
                account_id=account_id,
                peer_id=input_peer.user_id,
                peer_access_hash=input_peer.access_hash,
                peer_name=peer_name[:255],
                peer_username=getattr(peer, "username", None) or None,
                telegram_message_id=telegram_message_id,
                direction="out",
                body=auto_reply,
                is_read=True,
                created_at=sent_at,
            ))
            if conversation.updated_at < sent_at:
                conversation.updated_at = sent_at
            try:
                db.commit()
            except IntegrityError:
                db.rollback()

    @staticmethod
    def _claim_auto_reply(account_id: int, peer_id: int) -> tuple[str, datetime] | None:
        now = datetime.utcnow()
        with SessionLocal() as db:
            row = db.query(InboxConversation, User).join(
                User, User.id == InboxConversation.user_id
            ).filter(
                InboxConversation.account_id == account_id,
                InboxConversation.peer_id == peer_id,
                InboxConversation.is_blocked.is_(False),
                User.auto_reply_enabled.is_(True),
            ).with_for_update().first()
            if not row:
                return None
            conversation, user = row
            message = (user.auto_reply_message or "").strip()
            use_cooldown = (user.auto_reply_mode or "cooldown") != "always"
            if (
                not message
                or (
                    use_cooldown
                    and conversation.auto_replied_at
                    and conversation.auto_replied_at > now - AUTO_REPLY_COOLDOWN
                )
            ):
                return None
            conversation.auto_replied_at = now
            db.commit()
            return message, now

    async def send_reply(
        self,
        account_id: int,
        peer_id: int,
        peer_access_hash: int,
        body: str,
        file_path: str | None = None,
        voice_note: bool = False,
    ) -> tuple[int, datetime]:
        with SessionLocal() as db:
            account = db.get(TelegramAccount, account_id)
            if not account or not account.session_str:
                raise RuntimeError("Akun Telegram tidak tersedia")
            credentials = (account.session_str, account.api_id, account.api_hash)

        client = self.clients.get(account_id)
        temporary = not client or not client.is_connected()
        if temporary:
            client = TelegramClient(StringSession(credentials[0]), credentials[1], credentials[2])
            await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("Session akun Telegram sudah tidak valid")
            peer = types.InputPeerUser(peer_id, peer_access_hash)
            sent = await client.send_file(
                peer,
                file_path,
                caption=body or None,
                voice_note=voice_note,
            ) if file_path else await client.send_message(peer, body, link_preview=True)
            created_at = sent.date or datetime.utcnow()
            if created_at.tzinfo:
                created_at = created_at.replace(tzinfo=None)
            return sent.id, created_at
        finally:
            if temporary and client.is_connected():
                await client.disconnect()

    async def set_blocked_all(
        self,
        user_id: int,
        peer_id: int,
        peer_username: str | None,
        blocked: bool,
    ) -> tuple[set[int], int]:
        connected_ids = {
            account_id for account_id, client in self.clients.items()
            if client.is_connected()
        }
        if not connected_ids:
            return set(), 0

        with SessionLocal() as db:
            account_ids = [
                row[0] for row in db.query(TelegramAccount.id).filter(
                    TelegramAccount.id.in_(connected_ids),
                    TelegramAccount.user_id == user_id,
                    TelegramAccount.is_active == 1,
                    TelegramAccount.session_str.isnot(None),
                ).all()
            ]
            access_hashes = dict(
                db.query(InboxConversation.account_id, InboxConversation.peer_access_hash).filter(
                    InboxConversation.user_id == user_id,
                    InboxConversation.account_id.in_(account_ids),
                    InboxConversation.peer_id == peer_id,
                ).all()
            )

        semaphore = asyncio.Semaphore(8)

        async def update_account(account_id: int) -> int | None:
            client = self.clients.get(account_id)
            if not client or not client.is_connected():
                return None
            try:
                async with semaphore:
                    if account_id in access_hashes:
                        peer = types.InputPeerUser(peer_id, access_hashes[account_id])
                    elif peer_username:
                        peer = await client.get_input_entity(peer_username)
                    else:
                        return None
                    request = functions.contacts.BlockRequest if blocked else functions.contacts.UnblockRequest
                    await client(request(peer))
                return account_id
            except Exception:
                logger.exception("Contact block sync failed for account %s", account_id)
                return None

        results = await asyncio.gather(*(update_account(account_id) for account_id in account_ids))
        return {account_id for account_id in results if account_id is not None}, len(account_ids)

    async def download_avatar(
        self,
        account_id: int,
        peer_id: int,
        peer_access_hash: int,
        destination: Path,
    ) -> Path | None:
        client = self.clients.get(account_id)
        if not client or not client.is_connected():
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        downloaded = await client.download_profile_photo(
            types.InputPeerUser(peer_id, peer_access_hash), file=str(destination)
        )
        return Path(downloaded).resolve() if downloaded else None


inbox_manager = InboxManager()
