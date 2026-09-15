import asyncio
import json
import os
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable
from weakref import WeakValueDictionary

from sqlalchemy import func, or_
from telethon import TelegramClient
from telethon.errors import FloodWaitError, PeerFloodError
from telethon.sessions import StringSession

from app.database import SessionLocal
from app.models import BlastJob, BlastRecipient, TelegramAccount
from app.services.inbox_manager import inbox_manager


ACTIVE_JOB_STATES = {"queued", "running"}
MIN_SEND_DELAY_SECONDS = 0.0
PEER_FLOOD_COOLDOWN_SECONDS = int(os.getenv("PEER_FLOOD_COOLDOWN_SECONDS", "86400"))


class BlastManager:
    def __init__(self):
        # Weak maps release locks after the last waiter finishes. A long-lived
        # Railway process therefore does not retain every target ever seen.
        self.account_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
        self.target_locks: WeakValueDictionary[tuple[int, str], asyncio.Lock] = WeakValueDictionary()
        self.tasks: Dict[int, asyncio.Task] = {}
        self.enqueue_lock = asyncio.Lock()
        self._stop_flags: Dict[int, bool] = {}  # job_id → True berarti stop diminta
        self.account_pool_events: Dict[int, asyncio.Event] = {}

    # ─── Public API ──────────────────────────────────────────────────────────

    def start_job(self, job_id: int) -> None:
        current = self.tasks.get(job_id)
        if current and not current.done():
            return
        task = asyncio.create_task(self._run_job(job_id), name=f"blast-job-{job_id}")
        self.tasks[job_id] = task
        task.add_done_callback(lambda _task, jid=job_id: self.tasks.pop(jid, None))

    def stop_job(self, job_id: int) -> bool:
        """Minta job berhenti secara graceful. Return True jika job aktif."""
        if job_id in self.tasks and not self.tasks[job_id].done():
            self._stop_flags[job_id] = True
            return True
        return False

    def refresh_account_pool(self, user_id: int) -> None:
        self.account_pool_events.setdefault(user_id, asyncio.Event()).set()

    async def resume_incomplete_jobs(self) -> None:
        with SessionLocal() as db:
            jobs = db.query(BlastJob).filter(BlastJob.status.in_(list(ACTIVE_JOB_STATES))).all()
            job_ids = [job.id for job in jobs]
            if job_ids:
                db.query(BlastRecipient).filter(
                    BlastRecipient.job_id.in_(job_ids),
                    BlastRecipient.status == "sending",
                ).update(
                    {
                        BlastRecipient.status: "failed",
                        BlastRecipient.error: "Status tidak pasti setelah server restart; tidak dikirim ulang untuk mencegah duplikat",
                        BlastRecipient.updated_at: datetime.utcnow(),
                    },
                    synchronize_session=False,
                )
                db.query(BlastJob).filter(BlastJob.id.in_(job_ids)).update(
                    {BlastJob.status: "queued"}, synchronize_session=False
                )
                db.commit()

        for job_id in job_ids:
            self.start_job(job_id)

    # ─── Job runner ──────────────────────────────────────────────────────────

    async def _run_job(self, job_id: int) -> None:
        try:
            pool_event = None
            with SessionLocal() as db:
                job = db.get(BlastJob, job_id)
                if not job:
                    return
                job.status = "running"
                job.started_at = job.started_at or datetime.utcnow()
                db.commit()
                try:
                    selected_ids = list(dict.fromkeys(int(value) for value in json.loads(job.accounts_json)))
                except (TypeError, ValueError, json.JSONDecodeError):
                    selected_ids = []
                if not selected_ids:
                    selected_ids = [
                        row[0] for row in db.query(BlastRecipient.account_id)
                        .filter(BlastRecipient.job_id == job_id, BlastRecipient.account_id.isnot(None))
                        .distinct().all()
                    ]
                if job.source == "sheet":
                    pool_event = self.account_pool_events.setdefault(
                        job.user_id, asyncio.Event()
                    )
                    pool_event.clear()
                available_ids = self._available_account_ids(db, job, selected_ids)

            unavailable_ids = set()
            while available_ids and not self._stop_flags.get(job_id, False):
                account_id = available_ids[0]
                with SessionLocal() as db:
                    job = db.get(BlastJob, job_id)
                    recipients = db.query(BlastRecipient).filter(
                        BlastRecipient.job_id == job_id,
                        BlastRecipient.status == "pending",
                    ).order_by(BlastRecipient.sort_order).all()
                    if not recipients:
                        break
                    for recipient in recipients:
                        recipient.account_id = account_id
                    recipient_ids = [recipient.id for recipient in recipients]
                    db.commit()

                result = await self._run_account_queue(job_id, account_id, recipient_ids)
                pool_changed = bool(pool_event and pool_event.is_set())
                if pool_changed:
                    unavailable_ids.clear()
                    pool_event.clear()
                if result not in {"available", "refresh"}:
                    unavailable_ids.add(account_id)
                with SessionLocal() as db:
                    job = db.get(BlastJob, job_id)
                    refreshed_ids = self._available_account_ids(db, job, selected_ids)
                available_ids = [
                    account_id for account_id in refreshed_ids
                    if account_id not in unavailable_ids
                ]
                if self._stop_flags.get(job_id, False):
                    break

                with SessionLocal() as db:
                    paused = db.query(BlastRecipient).filter(
                        BlastRecipient.job_id == job_id,
                        BlastRecipient.status == "paused",
                    ).order_by(BlastRecipient.sort_order).all()
                    if paused and available_ids:
                        for recipient in paused:
                            recipient.account_id = available_ids[0]
                            recipient.status = "pending"
                            recipient.error = "Dialihkan otomatis ke akun berikutnya"
                            recipient.updated_at = datetime.utcnow()
                        self._refresh_counts(db, job_id)

            with SessionLocal() as db:
                remaining = db.query(BlastRecipient).filter(
                    BlastRecipient.job_id == job_id,
                    BlastRecipient.status == "pending",
                ).all()
                if remaining:
                    self._pause_many(db, [row.id for row in remaining], "Belum dikirim: semua akun tidak tersedia atau sedang dibatasi")
                    self._refresh_counts(db, job_id)
            self._finalize_job(job_id)
        except Exception as exc:
            with SessionLocal() as db:
                job = db.get(BlastJob, job_id)
                if job:
                    job.status = "failed"
                    job.completed_at = datetime.utcnow()
                    db.query(BlastRecipient).filter(
                        BlastRecipient.job_id == job_id,
                        BlastRecipient.status.in_(["pending", "sending"]),
                    ).update(
                        {BlastRecipient.status: "failed", BlastRecipient.error: f"Job error: {exc}"},
                        synchronize_session=False,
                    )
                    self._refresh_counts(db, job_id)
        finally:
            self._stop_flags.pop(job_id, None)
            self._cleanup_job_image(job_id)

    # ─── Per-account queue ───────────────────────────────────────────────────

    async def _run_account_queue(self, job_id: int, account_id: int, recipient_ids: Iterable[int]) -> str:
        recipient_ids = list(recipient_ids)
        account_lock = self.account_locks.get(account_id)
        if account_lock is None:
            account_lock = asyncio.Lock()
            self.account_locks[account_id] = account_lock

        async with account_lock:
            with SessionLocal() as db:
                account = db.get(TelegramAccount, account_id)
                job = db.get(BlastJob, job_id)
                if (
                    not account
                    or not account.session_str
                    or not job
                    or account.user_id != job.user_id
                    or (
                        account.blast_available_at
                        and account.blast_available_at > datetime.utcnow()
                    )
                ):
                    self._pause_many(db, recipient_ids, "Belum dikirim: akun tidak tersedia")
                    self._refresh_counts(db, job_id)
                    return "unavailable"

                account_snapshot = {
                    "session_str": account.session_str,
                    "api_id": account.api_id,
                    "api_hash": account.api_hash,
                    "last_blast_sent_at": account.last_blast_sent_at,
                }
                dynamic_pool = job.source == "sheet"
                pool_event = self.account_pool_events.setdefault(
                    job.user_id, asyncio.Event()
                ) if dynamic_pool else None
                message = job.message
                image_path = job.image_path
                stored_delay_min = (
                    job.delay_seconds
                    if job.delay_seconds is not None
                    else MIN_SEND_DELAY_SECONDS
                )
                delay_min = max(MIN_SEND_DELAY_SECONDS, float(stored_delay_min))
                # delay_max_seconds bisa None jika belum ada kolom (DB lama)
                delay_max_raw = getattr(job, "delay_max_seconds", None)
                delay_max = float(delay_max_raw) if delay_max_raw else delay_min
                if delay_max < delay_min:
                    delay_max = delay_min

            client = inbox_manager.clients.get(account_id)
            temporary_client = not client or not client.is_connected()
            if temporary_client:
                client = TelegramClient(
                    StringSession(account_snapshot["session_str"]),
                    account_snapshot["api_id"],
                    account_snapshot["api_hash"],
                )

            try:
                if temporary_client:
                    await client.connect()
                if temporary_client and not await client.is_user_authorized():
                    with SessionLocal() as db:
                        self._pause_many(db, recipient_ids, "Belum dikirim: session akun sudah tidak valid")
                        self._refresh_counts(db, job_id)
                    return "unavailable"

                ids = recipient_ids
                account_result = "available"

                last_sent_at = account_snapshot["last_blast_sent_at"]
                if last_sent_at:
                    remaining_delay = delay_min - (
                        datetime.utcnow() - last_sent_at
                    ).total_seconds()
                    if remaining_delay > 0:
                        if dynamic_pool and await self._wait_for_account_pool_change(
                            job.user_id, remaining_delay
                        ):
                            return "refresh"
                        if not dynamic_pool:
                            await asyncio.sleep(remaining_delay)

                for position, recipient_id in enumerate(ids):
                    if pool_event and pool_event.is_set():
                        account_result = "refresh"
                        break
                    # ── Cek stop flag ───────────────────────────────────────
                    if self._stop_flags.get(job_id, False):
                        remaining = ids[position:]
                        if remaining:
                            with SessionLocal() as db:
                                self._pause_many(db, remaining, "Dihentikan oleh pengguna")
                                self._refresh_counts(db, job_id)
                        account_result = "stopped"
                        break

                    send_result = await self._send_one(
                        client=client,
                        job_id=job_id,
                        recipient_id=recipient_id,
                        message=message,
                        image_path=image_path,
                    )

                    # Target gagal bukan berarti akun dibatasi; hanya flood yang
                    # memindahkan sisa antrean ke akun berikutnya.
                    if send_result in ("floodwait", "peerflood"):
                        # Dibatasi Telegram — hentikan seluruh antrean akun ini
                        remaining = ids[position + 1:]
                        if remaining:
                            reason = (
                                "Belum dikirim: akun sedang dibatasi Telegram (PeerFlood)"
                                if send_result == "peerflood"
                                else "Belum dikirim: Telegram meminta akun menunggu (FloodWait)"
                            )
                            with SessionLocal() as db:
                                self._pause_many(db, remaining, reason)
                                self._refresh_counts(db, job_id)
                        account_result = "limited"
                        break

                    # ── Jeda acak antar kiriman ─────────────────────────────
                    if position < len(ids) - 1:
                        actual_delay = random.uniform(delay_min, delay_max)
                        if actual_delay > 0:
                            if dynamic_pool and await self._wait_for_account_pool_change(
                                job.user_id, actual_delay
                            ):
                                account_result = "refresh"
                                break
                            if not dynamic_pool:
                                await asyncio.sleep(actual_delay)
                return account_result

            except Exception as exc:
                with SessionLocal() as db:
                    self._pause_many(db, recipient_ids, f"Belum dikirim: koneksi akun gagal ({exc})")
                    self._refresh_counts(db, job_id)
                return "unavailable"
            finally:
                if temporary_client:
                    await client.disconnect()

    # ─── Single send ─────────────────────────────────────────────────────────

    async def _send_one(
        self,
        client: TelegramClient,
        job_id: int,
        recipient_id: int,
        message: str,
        image_path: str | None,
    ) -> str:
        with SessionLocal() as db:
            recipient = db.get(BlastRecipient, recipient_id)
            if not recipient or recipient.status != "pending":
                return "continue"
            target = recipient.normalized_username
            job = db.get(BlastJob, job_id)
            if not job:
                return "continue"
            user_id = job.user_id
            account_id = recipient.account_id

        target_key = (user_id, target)
        target_lock = self.target_locks.get(target_key)
        if target_lock is None:
            target_lock = asyncio.Lock()
            self.target_locks[target_key] = target_lock

        async with target_lock:
            with SessionLocal() as db:
                recipient = db.get(BlastRecipient, recipient_id)
                job = db.get(BlastJob, job_id)
                if not recipient or not job or recipient.status != "pending":
                    return "continue"

                duplicate = (
                    db.query(BlastRecipient.id)
                    .join(BlastJob, BlastJob.id == BlastRecipient.job_id)
                    .filter(
                        BlastRecipient.normalized_username == recipient.normalized_username,
                        BlastRecipient.id != recipient.id,
                        BlastJob.user_id == job.user_id,
                        BlastRecipient.status == "sent",
                        BlastRecipient.sent_at >= job.created_at,
                    )
                    .first()
                )
                if duplicate:
                    self._finish_recipient(
                        db,
                        job_id,
                        recipient,
                        "skipped",
                        "Dilewati: username sudah dikirim oleh job lain yang berjalan bersamaan",
                    )
                    return "continue"

                recipient.status = "sending"
                recipient.error = None
                recipient.updated_at = datetime.utcnow()
                username = recipient.username
                # pending_count mencakup pending + sending, jadi transisi ini
                # cukup disimpan tanpa menghitung ulang seluruh job.
                db.commit()

            try:
                if image_path and Path(image_path).exists():
                    await client.send_file(username, image_path, caption=message, parse_mode="md")
                else:
                    await client.send_message(username, message, parse_mode="md", link_preview=True)

                with SessionLocal() as db:
                    recipient = db.get(BlastRecipient, recipient_id)
                    if recipient:
                        sent_at = datetime.utcnow()
                        recipient.sent_at = sent_at
                        account = db.get(TelegramAccount, account_id)
                        if account:
                            account.last_blast_sent_at = sent_at
                        self._finish_recipient(db, job_id, recipient, "sent")
                return "continue"

            except FloodWaitError as exc:
                secs = exc.seconds
                self._set_account_cooldown(account_id, secs)
                self._mark_paused(recipient_id, job_id, f"FloodWait {secs}s — dialihkan ke akun berikutnya")
                return "floodwait"
            except PeerFloodError:
                self._set_account_cooldown(account_id, PEER_FLOOD_COOLDOWN_SECONDS)
                self._mark_paused(recipient_id, job_id, "PeerFlood — dialihkan ke akun berikutnya")
                return "peerflood"
            except Exception as exc:
                # Gagal biasa — lanjut ke nomer berikutnya, hitung counter
                self._mark_failed(recipient_id, job_id, str(exc))
                return "failed"

    # ─── Helpers ─────────────────────────────────────────────────────────────

    async def _wait_for_account_pool_change(self, user_id: int, seconds: float) -> bool:
        event = self.account_pool_events.setdefault(user_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False

    @staticmethod
    def _available_account_ids(db, job: BlastJob, selected_ids: list[int]) -> list[int]:
        query = db.query(TelegramAccount.id).filter(
            TelegramAccount.user_id == job.user_id,
            TelegramAccount.is_active == 1,
            TelegramAccount.session_str.isnot(None),
            or_(
                TelegramAccount.blast_available_at.is_(None),
                TelegramAccount.blast_available_at <= datetime.utcnow(),
            ),
        )
        if job.source != "sheet":
            if not selected_ids:
                return []
            query = query.filter(TelegramAccount.id.in_(selected_ids))
            valid_ids = {row[0] for row in query}
            return [account_id for account_id in selected_ids if account_id in valid_ids]
        return [row[0] for row in query.order_by(TelegramAccount.id)]

    @staticmethod
    def _set_account_cooldown(account_id: int | None, seconds: int) -> None:
        if not account_id:
            return
        until = datetime.utcnow() + timedelta(seconds=max(1, seconds))
        with SessionLocal() as db:
            account = db.get(TelegramAccount, account_id)
            if account and (not account.blast_available_at or account.blast_available_at < until):
                account.blast_available_at = until
                db.commit()

    def _mark_failed(self, recipient_id: int, job_id: int, error: str) -> None:
        with SessionLocal() as db:
            recipient = db.get(BlastRecipient, recipient_id)
            if recipient:
                self._finish_recipient(db, job_id, recipient, "failed", error)

    def _mark_paused(self, recipient_id: int, job_id: int, error: str) -> None:
        with SessionLocal() as db:
            recipient = db.get(BlastRecipient, recipient_id)
            if recipient:
                recipient.status = "paused"
                recipient.error = error[:1000]
                recipient.updated_at = datetime.utcnow()
                db.commit()

    @staticmethod
    def _finish_recipient(
        db,
        job_id: int,
        recipient: BlastRecipient,
        status: str,
        error: str | None = None,
    ) -> None:
        counter = {
            "sent": BlastJob.sent_count,
            "failed": BlastJob.failed_count,
            "skipped": BlastJob.skipped_count,
        }[status]
        recipient.status = status
        recipient.error = error[:1000] if error else None
        recipient.updated_at = datetime.utcnow()
        db.query(BlastJob).filter(BlastJob.id == job_id).update(
            {
                counter: counter + 1,
                BlastJob.pending_count: BlastJob.pending_count - 1,
            },
            synchronize_session=False,
        )
        db.commit()

    @staticmethod
    def _pause_many(db, recipient_ids: Iterable[int], reason: str) -> None:
        ids = list(recipient_ids)
        if not ids:
            return
        db.query(BlastRecipient).filter(
            BlastRecipient.id.in_(ids),
            BlastRecipient.status == "pending",
        ).update(
            {
                BlastRecipient.status: "paused",
                BlastRecipient.error: reason[:1000],
                BlastRecipient.updated_at: datetime.utcnow(),
            },
            synchronize_session=False,
        )

    @staticmethod
    def _refresh_counts(db, job_id: int, *, commit: bool = True) -> None:
        # SessionLocal disables autoflush, so make pending recipient state
        # changes visible to the aggregate query before counting.
        db.flush()
        counts = dict(
            db.query(BlastRecipient.status, func.count(BlastRecipient.id))
            .filter(BlastRecipient.job_id == job_id)
            .group_by(BlastRecipient.status)
            .all()
        )
        job = db.get(BlastJob, job_id)
        if not job:
            if commit:
                db.commit()
            return
        job.sent_count = counts.get("sent", 0)
        job.failed_count = counts.get("failed", 0)
        job.skipped_count = counts.get("skipped", 0)
        job.pending_count = counts.get("pending", 0) + counts.get("sending", 0) + counts.get("paused", 0)
        job.total_count = sum(counts.values())
        if commit:
            db.commit()
        else:
            db.flush()

    def _finalize_job(self, job_id: int) -> None:
        with SessionLocal() as db:
            self._refresh_counts(db, job_id, commit=False)
            job = db.get(BlastJob, job_id)
            if not job:
                return
            if job.status == "paused":
                return  # sudah di-stop manual, jangan override
            paused_count = (
                db.query(func.count(BlastRecipient.id))
                .filter(BlastRecipient.job_id == job_id, BlastRecipient.status == "paused")
                .scalar()
                or 0
            )
            if paused_count > 0:
                job.status = "paused"
            else:
                job.status = "completed"
            job.completed_at = datetime.utcnow()
            db.commit()

    @staticmethod
    def _cleanup_job_image(job_id: int) -> None:
        with SessionLocal() as db:
            job = db.get(BlastJob, job_id)
            image_path = job.image_path if job else None
        if image_path and os.path.exists(image_path):
            try:
                os.unlink(image_path)
            except OSError:
                pass


blast_manager = BlastManager()
