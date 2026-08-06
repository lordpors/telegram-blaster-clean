import asyncio
import os
import random
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List

from sqlalchemy import func
from telethon import TelegramClient
from telethon.errors import FloodWaitError, PeerFloodError
from telethon.sessions import StringSession

from app.database import SessionLocal
from app.models import BlastJob, BlastRecipient, TelegramAccount


TERMINAL_RECIPIENT_STATES = {"sent", "failed", "skipped", "paused"}
ACTIVE_JOB_STATES = {"queued", "running"}
MAX_CONSECUTIVE_FAILURES = 5  # Berhenti per-akun setelah gagal berturut-turut


class BlastManager:
    def __init__(self):
        self.account_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.target_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.tasks: Dict[int, asyncio.Task] = {}
        self.enqueue_lock = asyncio.Lock()
        self._stop_flags: Dict[int, bool] = {}  # job_id → True berarti stop diminta

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
            with SessionLocal() as db:
                job = db.get(BlastJob, job_id)
                if not job:
                    return
                job.status = "running"
                job.started_at = job.started_at or datetime.utcnow()
                db.commit()

                rows = (
                    db.query(BlastRecipient.account_id, BlastRecipient.id)
                    .filter(BlastRecipient.job_id == job_id, BlastRecipient.status == "pending")
                    .order_by(BlastRecipient.account_id, BlastRecipient.sort_order)
                    .all()
                )

            grouped: Dict[int, List[int]] = defaultdict(list)
            for account_id, recipient_id in rows:
                if account_id is not None:
                    grouped[account_id].append(recipient_id)

            await asyncio.gather(
                *(self._run_account_queue(job_id, account_id, recipient_ids)
                  for account_id, recipient_ids in grouped.items()),
                return_exceptions=True,
            )
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
                    db.commit()
                    self._refresh_counts(db, job_id)
        finally:
            self._stop_flags.pop(job_id, None)
            self._cleanup_job_image(job_id)

    # ─── Per-account queue ───────────────────────────────────────────────────

    async def _run_account_queue(self, job_id: int, account_id: int, recipient_ids: Iterable[int]) -> None:
        async with self.account_locks[account_id]:
            with SessionLocal() as db:
                account = db.get(TelegramAccount, account_id)
                job = db.get(BlastJob, job_id)
                if not account or not account.session_str or not job:
                    self._fail_many(db, recipient_ids, "Akun tidak ditemukan atau session Telegram kosong")
                    self._refresh_counts(db, job_id)
                    return

                account_snapshot = {
                    "session_str": account.session_str,
                    "api_id": account.api_id,
                    "api_hash": account.api_hash,
                }
                message = job.message
                image_path = job.image_path
                delay_min = max(0.5, float(job.delay_seconds or 5))
                # delay_max_seconds bisa None jika belum ada kolom (DB lama)
                delay_max_raw = getattr(job, "delay_max_seconds", None)
                delay_max = float(delay_max_raw) if delay_max_raw else delay_min
                if delay_max < delay_min:
                    delay_max = delay_min

            client = TelegramClient(
                StringSession(account_snapshot["session_str"]),
                account_snapshot["api_id"],
                account_snapshot["api_hash"],
            )

            try:
                await client.connect()
                if not await client.is_user_authorized():
                    with SessionLocal() as db:
                        self._fail_many(db, recipient_ids, "Session akun sudah tidak valid; hubungkan ulang akun")
                        self._refresh_counts(db, job_id)
                    return

                ids = list(recipient_ids)
                consecutive_failures = 0

                for position, recipient_id in enumerate(ids):
                    # ── Cek stop flag ───────────────────────────────────────
                    if self._stop_flags.get(job_id, False):
                        remaining = ids[position:]
                        if remaining:
                            with SessionLocal() as db:
                                self._pause_many(db, remaining, "Dihentikan oleh pengguna")
                                self._refresh_counts(db, job_id)
                        break

                    send_result = await self._send_one(
                        client=client,
                        job_id=job_id,
                        recipient_id=recipient_id,
                        message=message,
                        image_path=image_path,
                    )

                    if send_result == "continue":
                        # Berhasil / skipped — reset counter kegagalan
                        consecutive_failures = 0

                    elif send_result == "failed":
                        # Gagal biasa — lanjut ke berikutnya, tambah counter
                        consecutive_failures += 1
                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            remaining = ids[position + 1:]
                            if remaining:
                                with SessionLocal() as db:
                                    self._pause_many(
                                        db, remaining,
                                        f"Dihentikan: {MAX_CONSECUTIVE_FAILURES} kegagalan berturut-turut"
                                    )
                                    self._refresh_counts(db, job_id)
                            break

                    elif send_result in ("floodwait", "peerflood"):
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
                        break

                    # ── Jeda acak antar kiriman ─────────────────────────────
                    if position < len(ids) - 1:
                        actual_delay = random.uniform(delay_min, delay_max)
                        await asyncio.sleep(actual_delay)

            except Exception as exc:
                with SessionLocal() as db:
                    self._fail_many(db, recipient_ids, f"Koneksi akun gagal: {exc}", only_active=True)
                    self._refresh_counts(db, job_id)
            finally:
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

        async with self.target_locks[target]:
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
                        BlastRecipient.status == "sent",
                        BlastRecipient.sent_at >= job.created_at,
                    )
                    .first()
                )
                if duplicate:
                    recipient.status = "skipped"
                    recipient.error = "Dilewati: username sudah dikirim oleh job lain yang berjalan bersamaan"
                    recipient.updated_at = datetime.utcnow()
                    db.commit()
                    self._refresh_counts(db, job_id)
                    return "continue"

                recipient.status = "sending"
                recipient.error = None
                recipient.updated_at = datetime.utcnow()
                username = recipient.username
                db.commit()
                self._refresh_counts(db, job_id)

            try:
                if image_path and Path(image_path).exists():
                    await client.send_file(username, image_path, caption=message, parse_mode="md")
                else:
                    await client.send_message(username, message, parse_mode="md", link_preview=True)

                with SessionLocal() as db:
                    recipient = db.get(BlastRecipient, recipient_id)
                    if recipient:
                        recipient.status = "sent"
                        recipient.error = None
                        recipient.sent_at = datetime.utcnow()
                        recipient.updated_at = datetime.utcnow()
                        db.commit()
                        self._refresh_counts(db, job_id)
                return "continue"

            except FloodWaitError as exc:
                secs = exc.seconds
                self._mark_failed(recipient_id, job_id, f"FloodWait {secs}s — menunggu lalu lanjut")
                await asyncio.sleep(secs)
                return "failed"
            except PeerFloodError:
                self._mark_failed(recipient_id, job_id, "PeerFlood — lanjut ke nomor berikutnya")
                return "failed"
            except Exception as exc:
                # Gagal biasa — lanjut ke nomer berikutnya, hitung counter
                self._mark_failed(recipient_id, job_id, str(exc))
                return "failed"

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _mark_failed(self, recipient_id: int, job_id: int, error: str) -> None:
        with SessionLocal() as db:
            recipient = db.get(BlastRecipient, recipient_id)
            if recipient:
                recipient.status = "failed"
                recipient.error = error[:1000]
                recipient.updated_at = datetime.utcnow()
                db.commit()
            self._refresh_counts(db, job_id)

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
        db.commit()

    @staticmethod
    def _fail_many(db, recipient_ids: Iterable[int], error: str, only_active: bool = False) -> None:
        ids = list(recipient_ids)
        if not ids:
            return
        query = db.query(BlastRecipient).filter(BlastRecipient.id.in_(ids))
        if only_active:
            query = query.filter(BlastRecipient.status.in_(["pending", "sending"]))
        else:
            query = query.filter(BlastRecipient.status == "pending")
        query.update(
            {
                BlastRecipient.status: "failed",
                BlastRecipient.error: error[:1000],
                BlastRecipient.updated_at: datetime.utcnow(),
            },
            synchronize_session=False,
        )
        db.commit()

    @staticmethod
    def _refresh_counts(db, job_id: int) -> None:
        counts = dict(
            db.query(BlastRecipient.status, func.count(BlastRecipient.id))
            .filter(BlastRecipient.job_id == job_id)
            .group_by(BlastRecipient.status)
            .all()
        )
        job = db.get(BlastJob, job_id)
        if not job:
            return
        job.sent_count = counts.get("sent", 0)
        job.failed_count = counts.get("failed", 0)
        job.skipped_count = counts.get("skipped", 0)
        job.pending_count = counts.get("pending", 0) + counts.get("sending", 0) + counts.get("paused", 0)
        job.total_count = sum(counts.values())
        db.commit()

    def _finalize_job(self, job_id: int) -> None:
        with SessionLocal() as db:
            self._refresh_counts(db, job_id)
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
            finished_without_failure = job.sent_count + job.skipped_count
            if paused_count > 0:
                job.status = "paused"
            elif job.total_count > 0 and finished_without_failure == job.total_count and job.failed_count == 0:
                job.status = "completed"
            elif job.sent_count > 0 or job.skipped_count > 0:
                job.status = "partial"
            else:
                job.status = "failed"
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
