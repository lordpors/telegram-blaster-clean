import asyncio
import json
import math
import os
from datetime import datetime, timedelta

import httpx
from sqlalchemy import or_

from app.database import SessionLocal
from app.models import BlastJob, BlastRecipient, TelegramAccount, User
from app.routers.telegram import _normalize_delay_range, _normalize_username
from app.services.blast_manager import blast_manager


TERMINAL_STATES = {"completed", "partial", "failed", "cancelled"}


class SheetBlaster:
    def __init__(self):
        self.task = None
        self.url = os.getenv("SHEET_BLAST_WEBAPP_URL", "").strip()
        self.secret = os.getenv("SHEET_BLAST_SECRET", "").strip()
        self.owner = os.getenv("SHEET_BLAST_OWNER", "porscy").strip().casefold()
        self.poll_seconds = max(10, int(os.getenv("SHEET_BLAST_POLL_SECONDS", "30")))
        self.retry_seconds = max(60, int(os.getenv("SHEET_BLAST_RETRY_SECONDS", "300")))

    def start(self):
        if not self.url or not self.secret or (self.task and not self.task.done()):
            return
        self.task = asyncio.create_task(self._run(), name="sheet-blaster")

    async def shutdown(self):
        if not self.task:
            return
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    async def _run(self):
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            while True:
                try:
                    changed = await self.sync_once(client)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(f"Sheet blaster menunggu setelah gagal sinkron: {exc}")
                    changed = False
                await asyncio.sleep(1 if changed else self.poll_seconds)

    async def sync_once(self, client):
        with SessionLocal() as db:
            user = db.query(User).filter(
                User.username == self.owner,
                User.is_active.is_(True),
            ).first()
            if not user:
                return False

            job = db.query(BlastJob).filter(
                BlastJob.user_id == user.id,
                BlastJob.source == "sheet",
                BlastJob.sheet_synced_at.is_(None),
            ).order_by(BlastJob.created_at).first()

            if job:
                final = job.status in TERMINAL_STATES
                changed = False
                if not final:
                    changed = await self._sync_settings(client, db, job)
                changed = await self._sync_results(client, db, job, final=final) or changed
                if final:
                    remaining = db.query(BlastRecipient.id).filter(
                        BlastRecipient.job_id == job.id,
                        BlastRecipient.sheet_item_id.isnot(None),
                        BlastRecipient.sheet_synced_at.is_(None),
                    ).first()
                    if not remaining:
                        job.sheet_synced_at = datetime.utcnow()
                        db.commit()
                    return True

            if job and job.status == "paused":
                age = datetime.utcnow() - (job.completed_at or job.created_at)
                if age >= timedelta(seconds=self.retry_seconds):
                    db.query(BlastRecipient).filter(
                        BlastRecipient.job_id == job.id,
                        BlastRecipient.status == "paused",
                    ).update({BlastRecipient.status: "pending", BlastRecipient.error: None})
                    job.status = "queued"
                    job.completed_at = None
                    db.commit()
                    blast_manager.start_job(job.id)
                    return True
                return changed

            if job:
                return changed

            account_ids = [row[0] for row in db.query(TelegramAccount.id).filter(
                TelegramAccount.user_id == user.id,
                TelegramAccount.is_active == 1,
                TelegramAccount.session_str.isnot(None),
                or_(
                    TelegramAccount.blast_available_at.is_(None),
                    TelegramAccount.blast_available_at <= datetime.utcnow(),
                ),
            ).order_by(TelegramAccount.id)]
            user_id = user.id
            if not account_ids:
                return False
            last_completed = db.query(BlastJob.completed_at).filter(
                BlastJob.user_id == user.id,
                BlastJob.source == "sheet",
                BlastJob.completed_at.isnot(None),
            ).order_by(BlastJob.completed_at.desc()).first()
            last_completed_at = last_completed[0] if last_completed else None

        settings = await self._fetch_settings(client)
        delay_minutes = self._job_delay_minutes(settings)
        if self._waiting_for_next_job(last_completed_at, delay_minutes):
            return False

        response = await client.post(
            self.url,
            json={"secret": self.secret, "action": "claim"},
        )
        response.raise_for_status()
        rows = response.json().get("items", [])
        if rows:
            await self._create_job(client, user_id, account_ids, rows)
            return True
        return False

    async def _fetch_settings(self, client):
        response = await client.post(
            self.url,
            json={"secret": self.secret, "action": "settings"},
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _job_delay_minutes(payload):
        try:
            value = float(payload.get("job_delay_minutes", 0))
        except (TypeError, ValueError):
            return 0
        return value if math.isfinite(value) and value > 0 else 0

    @staticmethod
    def _waiting_for_next_job(completed_at, delay_minutes, now=None):
        return bool(
            completed_at
            and (now or datetime.utcnow())
            < completed_at + timedelta(minutes=delay_minutes)
        )

    async def _sync_settings(self, client, db, job):
        payload = await self._fetch_settings(client)
        if "interval" not in payload:
            return False
        delay, _ = _normalize_delay_range(payload["interval"], payload["interval"])
        if job.delay_seconds == delay and job.delay_max_seconds == delay:
            return False
        job.delay_seconds = delay
        job.delay_max_seconds = delay
        db.commit()
        blast_manager.refresh_account_pool(job.user_id)
        return True

    async def _sync_results(self, client, db, job, *, final):
        statuses = ["sent", "failed", "skipped"] if final else ["sent"]
        rows = db.query(BlastRecipient).filter(
            BlastRecipient.job_id == job.id,
            BlastRecipient.sheet_item_id.isnot(None),
            BlastRecipient.sheet_synced_at.is_(None),
            BlastRecipient.status.in_(statuses),
        ).all()
        if not rows:
            return False
        results = [{
            "id": row.sheet_item_id,
            "status": row.status,
            "error": row.error or "",
            "message": job.message,
            "interval": job.delay_seconds,
        } for row in rows]
        response = await client.post(
            self.url,
            json={"secret": self.secret, "action": "finish", "results": results},
        )
        response.raise_for_status()
        if response.json().get("ok") is not True:
            raise RuntimeError("Apps Script tidak mengonfirmasi sinkronisasi")
        now = datetime.utcnow()
        for row in rows:
            row.sheet_synced_at = now
        db.commit()
        return True

    async def _create_job(self, client, user_id, account_ids, rows):
        valid = []
        rejected = []
        seen = set()
        message = str(rows[0].get("message", "")).strip()
        delay, _ = _normalize_delay_range(rows[0].get("interval", 0), rows[0].get("interval", 0))

        for row in rows:
            item_id = str(row.get("id", ""))
            normalized = _normalize_username(str(row.get("username", "")))
            same_settings = (
                str(row.get("message", "")).strip() == message
                and _normalize_delay_range(row.get("interval", 0), row.get("interval", 0))[0] == delay
            )
            if not item_id:
                continue
            if not normalized or not message or len(message) > 4096 or not same_settings:
                rejected.append({"id": item_id, "status": "failed", "error": "Data username, pesan, atau interval tidak valid"})
                continue
            display, key = normalized
            if key in seen:
                rejected.append({
                    "id": item_id,
                    "status": "failed",
                    "error": "Username duplikat masih menunggu baris pertama",
                })
                continue
            seen.add(key)
            valid.append((item_id, display, key))

        if rejected:
            response = await client.post(
                self.url,
                json={"secret": self.secret, "action": "finish", "results": rejected},
            )
            response.raise_for_status()
            if response.json().get("ok") is not True:
                raise RuntimeError("Apps Script tidak mengonfirmasi baris yang ditolak")
        if not valid:
            return

        async with blast_manager.enqueue_lock:
            with SessionLocal() as db:
                sent_keys = {
                    row[0] for row in db.query(BlastRecipient.normalized_username)
                    .join(BlastJob, BlastJob.id == BlastRecipient.job_id)
                    .filter(
                        BlastRecipient.normalized_username.in_([row[2] for row in valid]),
                        BlastRecipient.status == "sent",
                        BlastJob.source == "sheet",
                        BlastJob.user_id == user_id,
                    )
                }
                already_sent = [
                    {
                        "id": item_id,
                        "status": "sent",
                        "error": "Sudah terkirim dari antrean Sheet",
                        "message": message,
                        "interval": delay,
                    }
                    for item_id, _display, key in valid if key in sent_keys
                ]
                valid = [row for row in valid if row[2] not in sent_keys]
                if already_sent:
                    response = await client.post(
                        self.url,
                        json={"secret": self.secret, "action": "finish", "results": already_sent},
                    )
                    response.raise_for_status()
                    if response.json().get("ok") is not True:
                        raise RuntimeError("Apps Script tidak mengonfirmasi username duplikat")
                if not valid:
                    return
                job = BlastJob(
                    user_id=user_id,
                    status="queued",
                    source="sheet",
                    message=message,
                    delay_seconds=delay,
                    delay_max_seconds=delay,
                    accounts_json=json.dumps(account_ids),
                    consent_confirmed=True,
                    total_count=len(valid),
                    pending_count=len(valid),
                )
                db.add(job)
                db.flush()
                for index, (item_id, display, key) in enumerate(valid):
                    db.add(BlastRecipient(
                        job_id=job.id,
                        account_id=account_ids[index % len(account_ids)],
                        sheet_item_id=item_id,
                        username=display,
                        normalized_username=key,
                        sort_order=index,
                    ))
                db.commit()
                job_id = job.id

        blast_manager.start_job(job_id)


sheet_blaster = SheetBlaster()
