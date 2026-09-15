import base64
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import unittest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timedelta
from types import SimpleNamespace

from itsdangerous import TimestampSigner


TEST_DB = Path("/tmp/porslabs-multitenancy-test.db")
TEST_CSRF = "test-csrf-token"
TEST_DB.unlink(missing_ok=True)
os.environ["BLASTER_DB_PATH"] = str(TEST_DB)
os.environ["SESSION_SECRET"] = "test-session-secret-for-porslabs"
os.environ["DATA_ENCRYPTION_KEY"] = "test-data-encryption-key-for-porslabs"
os.environ["BOOTSTRAP_OWNER_EMAIL"] = "owner@example.com"
os.environ["INBOX_LISTENERS_ENABLED"] = "false"
os.environ["OFFICE_STATS_KEY"] = "office-stats-test-key"
os.environ.pop("APP_PASSWORD", None)
os.environ.pop("GOOGLE_CLIENT_ID", None)
os.environ.pop("GOOGLE_CLIENT_SECRET", None)

from fastapi.testclient import TestClient  # noqa: E402
from telethon import types  # noqa: E402
from telethon.errors import AuthKeyDuplicatedError  # noqa: E402

from app.auth import hash_password, upsert_google_user, verify_password  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import BlastJob, BlastRecipient, DeviceSession, InboxConversation, InboxMessage, TelegramAccount, User  # noqa: E402
from app.security import ENCRYPTED_PREFIX, decrypt_sensitive, encrypt_sensitive  # noqa: E402
from app.routers import scraper  # noqa: E402
from app.routers.telegram import _normalize_delay_range, _normalize_username  # noqa: E402
from app.services.blast_manager import blast_manager  # noqa: E402
from app.services.inbox_manager import inbox_manager  # noqa: E402
from app.services.sheet_blaster import SheetBlaster  # noqa: E402
from app.template_utils import jakarta_time  # noqa: E402


def _session_cookie(user_id: int) -> str:
    payload = base64.b64encode(
        json.dumps({
            "user_id": user_id,
            "csrf_token": TEST_CSRF,
            "expires_at": int(time.time()) + 3600,
        }).encode("utf-8")
    )
    return TimestampSigner(os.environ["SESSION_SECRET"]).sign(payload).decode("utf-8")


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if not match:
        raise AssertionError("CSRF token not found")
    return match.group(1)


class TenantIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with SessionLocal() as db:
            cls.owner = db.query(User).filter(User.email == "owner@example.com").one()
            cls.other = User(
                google_sub="google-other-user",
                email="other@example.com",
                name="Other User",
            )
            db.add(cls.other)
            db.flush()

            cls.owner_account = TelegramAccount(
                user_id=cls.owner.id,
                label="Owner Account",
                phone="+620000000001",
                session_str="owner-session",
                api_id=1,
                api_hash="owner-hash",
                is_active=1,
            )
            cls.other_account = TelegramAccount(
                user_id=cls.other.id,
                label="Other Secret Account",
                phone="+620000000002",
                session_str="other-session",
                api_id=2,
                api_hash="other-hash",
                is_active=1,
            )
            db.add_all([cls.owner_account, cls.other_account])
            db.flush()

            cls.owner_job = BlastJob(
                user_id=cls.owner.id,
                status="completed",
                message="owner job",
                accounts_json="[]",
                consent_confirmed=True,
            )
            cls.other_job = BlastJob(
                user_id=cls.other.id,
                status="completed",
                message="other secret job",
                accounts_json="[]",
                consent_confirmed=True,
            )
            db.add_all([cls.owner_job, cls.other_job])
            db.commit()
            for item in (cls.owner, cls.other, cls.owner_account, cls.other_account, cls.owner_job, cls.other_job):
                db.refresh(item)
            cls.owner_id = cls.owner.id
            cls.other_id = cls.other.id
            cls.owner_account_id = cls.owner_account.id
            cls.other_account_id = cls.other_account.id
            cls.other_job_id = cls.other_job.id

    def setUp(self):
        self.client = TestClient(app)
        self.client.cookies.set("porslabs_session", _session_cookie(self.owner_id))

    def tearDown(self):
        self.client.close()

    def test_dashboard_only_renders_current_users_data(self):
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Owner Account", response.text)
        self.assertIn(f'data-account-status="{self.owner_account_id}"', response.text)
        self.assertIn("data-account-status-label>Memeriksa…", response.text)
        self.assertIn('id="dashboard-account-search"', response.text)
        self.assertNotIn("Other Secret Account", response.text)
        self.assertNotIn("other secret job", response.text)

    def test_office_account_stats_are_protected_and_owner_only(self):
        self.owner_account.blast_available_at = datetime.utcnow() + timedelta(hours=1)
        with SessionLocal() as db:
            account = db.get(TelegramAccount, self.owner_account_id)
            account.blast_available_at = self.owner_account.blast_available_at
            db.commit()
        try:
            self.assertEqual(self.client.get("/api/office/account-stats").status_code, 404)
            with SessionLocal() as db:
                owner_accounts = db.query(TelegramAccount).filter(
                    TelegramAccount.user_id == self.owner_id
                )
                expected = {
                    "active": owner_accounts.filter(
                        TelegramAccount.is_active == 1,
                        TelegramAccount.session_str.isnot(None),
                        (TelegramAccount.blast_available_at.is_(None))
                        | (TelegramAccount.blast_available_at <= datetime.utcnow()),
                    ).count(),
                    "flood": owner_accounts.filter(
                        TelegramAccount.is_active == 1,
                        TelegramAccount.session_str.isnot(None),
                        TelegramAccount.blast_available_at > datetime.utcnow(),
                    ).count(),
                    "total": owner_accounts.count(),
                }
                all_accounts = db.query(TelegramAccount).count()
            response = self.client.get(
                "/api/office/account-stats",
                headers={"x-office-key": "office-stats-test-key"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), expected)
            self.assertLess(expected["total"], all_accounts)
        finally:
            with SessionLocal() as db:
                db.get(TelegramAccount, self.owner_account_id).blast_available_at = None
                db.commit()
    def test_visible_times_use_jakarta_timezone(self):
        self.assertEqual(jakarta_time(datetime(2026, 9, 10, 8, 20), "%H:%M"), "15:20")

    def test_zero_delay_is_available_and_preserved(self):
        response = self.client.get("/blast")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.count('min="0" max="3600"'), 2)
        self.assertEqual(_normalize_delay_range(0, 0), (0.0, 0.0))
        self.assertEqual(_normalize_delay_range(0.25, 0.75), (0.25, 0.75))
        self.assertEqual(_normalize_delay_range(-5, -1), (0.0, 0.0))
        self.assertIn("maksimal 500", response.text)
        self.assertIn("<th>Gagal</th><th>Dilewati</th>", response.text)

        over_limit = self.client.post("/send-blast", data={
            "csrf_token": _csrf_from(response.text),
            "account_ids": str(self.owner_account_id),
            "usernames": "\n".join(f"target_{index}" for index in range(501)),
            "message": "Batas aman",
            "consent_confirmed": "true",
        })
        self.assertIn("Maksimal 500 username", over_limit.text)

    def test_blast_target_rejects_values_that_do_not_fit_the_database(self):
        self.assertIsNone(_normalize_username("x" * 256))
        self.assertIsNone(_normalize_username("invalid target"))
        self.assertEqual(_normalize_username("https://t.me/Valid_Target?start=1"), ("Valid_Target", "valid_target"))

    def test_paused_sheet_job_can_be_stopped(self):
        with SessionLocal() as db:
            job = BlastJob(
                user_id=self.owner_id,
                status="paused",
                source="sheet",
                message="stop me",
                accounts_json=f"[{self.owner_account_id}]",
                consent_confirmed=True,
            )
            db.add(job)
            db.flush()
            db.add(BlastRecipient(
                job_id=job.id,
                account_id=self.owner_account_id,
                username="waiting_target",
                normalized_username="waiting_target",
                status="paused",
            ))
            db.commit()
            job_id = job.id

        try:
            page = self.client.get(f"/blast?job_id={job_id}")
            self.assertIn(f'action="/stop-job/{job_id}"', page.text)
            response = self.client.post(
                f"/stop-job/{job_id}",
                data={"csrf_token": _csrf_from(page.text)},
            )
            self.assertEqual(response.status_code, 200)
            with SessionLocal() as db:
                self.assertEqual(db.get(BlastJob, job_id).status, "cancelled")
                self.assertEqual(
                    db.query(BlastRecipient).filter(BlastRecipient.job_id == job_id).one().status,
                    "skipped",
                )
            self.assertTrue(self.client.get(f"/api/jobs/{job_id}").json()["terminal"])
        finally:
            with SessionLocal() as db:
                db.query(BlastRecipient).filter(BlastRecipient.job_id == job_id).delete()
                db.query(BlastJob).filter(BlastJob.id == job_id).delete()
                db.commit()

    def test_blast_account_picker_collapses_after_twelve(self):
        with SessionLocal() as db:
            extras = [
                TelegramAccount(
                    user_id=self.owner_id,
                    label=f"Extra Account {index}",
                    phone=f"+6299000000{index:02d}",
                    session_str=f"extra-session-{index}",
                    api_id=10 + index,
                    api_hash=f"extra-hash-{index}",
                    is_active=1,
                )
                for index in range(12)
            ]
            db.add_all(extras)
            db.commit()
            extra_ids = [account.id for account in extras]

        try:
            response = self.client.get("/blast")
        finally:
            with SessionLocal() as db:
                db.query(TelegramAccount).filter(TelegramAccount.id.in_(extra_ids)).delete(
                    synchronize_session=False
                )
                db.commit()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.count('class="account-choice"'), 13)
        self.assertEqual(len(re.findall(r'<label class="account-choice"[^>]* hidden', response.text)), 1)
        self.assertIn('id="show-more-accounts"', response.text)
        self.assertIn('id="show-all-accounts"', response.text)
        self.assertIn('id="hide-accounts" hidden', response.text)
        self.assertIn('id="blast-account-search"', response.text)
        self.assertIn('class="flex pagination-controls"', response.text)
        self.assertIn('class="card blast-account-card"', response.text)
        self.assertIn("Konten & Target Pesan", response.text)
        self.assertEqual(response.text.count('data-account-status="'), 13)
        self.assertIn('id="use-all-accounts"', response.text)
        self.assertIn("connected_account_ids", response.text)
        self.assertLess(response.text.index("Gunakan semua akun"), response.text.index('id="blast-account-grid"'))

    def test_blast_failover_moves_paused_targets_to_the_next_account(self):
        with SessionLocal() as db:
            backup_account = TelegramAccount(
                user_id=self.owner_id,
                label="Failover Account",
                phone="+629988776655",
                session_str="failover-session",
                api_id=99,
                api_hash="failover-hash",
                is_active=1,
            )
            db.add(backup_account)
            db.flush()
            job = BlastJob(
                user_id=self.owner_id,
                status="queued",
                message="failover test",
                accounts_json=json.dumps([self.owner_account_id, backup_account.id]),
                consent_confirmed=True,
                total_count=4,
                pending_count=4,
            )
            db.add(job)
            db.flush()
            recipients = [
                BlastRecipient(
                    job_id=job.id,
                    account_id=(self.owner_account_id if index % 2 == 0 else backup_account.id),
                    username=f"failover_target_{index}",
                    normalized_username=f"failover_target_{index}",
                    sort_order=index,
                )
                for index in range(4)
            ]
            db.add_all(recipients)
            db.commit()
            job_id = job.id
            backup_account_id = backup_account.id

        calls = []

        async def run_queue(_job_id, account_id, recipient_ids):
            calls.append((account_id, len(recipient_ids)))
            with SessionLocal() as db:
                rows = db.query(BlastRecipient).filter(BlastRecipient.id.in_(recipient_ids)).all()
                if account_id == self.owner_account_id:
                    for row in rows:
                        row.status = "paused"
                        row.error = "FloodWait — dialihkan"
                    blast_manager._refresh_counts(db, _job_id)
                    return "limited"
                for row in rows:
                    row.status = "sent"
                    row.error = None
                    row.sent_at = datetime.utcnow()
                blast_manager._refresh_counts(db, _job_id)
                return "available"

        try:
            with patch.object(blast_manager, "_run_account_queue", new=AsyncMock(side_effect=run_queue)):
                asyncio.run(blast_manager._run_job(job_id))
            with SessionLocal() as db:
                job = db.get(BlastJob, job_id)
                rows = db.query(BlastRecipient).filter(BlastRecipient.job_id == job_id).all()
                self.assertEqual(job.status, "completed")
                self.assertTrue(all(row.status == "sent" for row in rows))
                self.assertTrue(all(row.account_id == backup_account_id for row in rows))
                self.assertEqual(calls, [
                    (self.owner_account_id, 4),
                    (backup_account_id, 4),
                ])
        finally:
            with SessionLocal() as db:
                db.query(BlastJob).filter(BlastJob.id == job_id).delete()
                db.query(TelegramAccount).filter(TelegramAccount.id == backup_account_id).delete()
                db.commit()

    def test_blast_reuses_the_connected_inbox_client(self):
        with SessionLocal() as db:
            job = BlastJob(
                user_id=self.owner_id,
                status="running",
                message="reuse client test",
                accounts_json=json.dumps([self.owner_account_id]),
                consent_confirmed=True,
            )
            db.add(job)
            db.flush()
            recipient = BlastRecipient(
                job_id=job.id,
                account_id=self.owner_account_id,
                username="reuse_client_target",
                normalized_username="reuse_client_target",
            )
            db.add(recipient)
            db.commit()
            job_id = job.id
            recipient_id = recipient.id

        class ConnectedClient:
            disconnected = False

            def is_connected(self):
                return True

            async def disconnect(self):
                self.disconnected = True

        connected_client = ConnectedClient()
        try:
            with (
                patch.object(inbox_manager, "clients", {self.owner_account_id: connected_client}),
                patch("app.services.blast_manager.TelegramClient") as client_factory,
                patch.object(blast_manager, "_send_one", AsyncMock(return_value="continue")) as sender,
            ):
                result = asyncio.run(blast_manager._run_account_queue(
                    job_id, self.owner_account_id, [recipient_id]
                ))
            self.assertEqual(result, "available")
            client_factory.assert_not_called()
            self.assertFalse(connected_client.disconnected)
            self.assertIs(sender.await_args.kwargs["client"], connected_client)
        finally:
            with SessionLocal() as db:
                db.query(BlastJob).filter(BlastJob.id == job_id).delete()
                db.commit()

    def test_sheet_results_sync_immediately_and_account_cooldown_is_persisted(self):
        with SessionLocal() as db:
            job = BlastJob(
                user_id=self.owner_id,
                source="sheet",
                status="running",
                message="sheet test",
                accounts_json=json.dumps([self.owner_account_id]),
                total_count=1,
            )
            db.add(job)
            db.flush()
            recipient = BlastRecipient(
                job_id=job.id,
                account_id=self.owner_account_id,
                sheet_item_id="sheet-test-item",
                username="sheet_test_target",
                normalized_username="sheet_test_target",
                status="sent",
            )
            db.add(recipient)
            db.commit()
            job_id = job.id

        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"ok": True},
        )
        client = SimpleNamespace(post=AsyncMock(return_value=response))
        worker = SheetBlaster()
        worker.url = "https://example.invalid/exec"
        worker.secret = "test-secret"
        try:
            with SessionLocal() as db:
                job = db.get(BlastJob, job_id)
                self.assertTrue(asyncio.run(worker._sync_results(client, db, job, final=False)))
                self.assertIsNotNone(job.recipients[0].sheet_synced_at)
            blast_manager._set_account_cooldown(self.owner_account_id, 60)
            with SessionLocal() as db:
                account = db.get(TelegramAccount, self.owner_account_id)
                self.assertGreater(account.blast_available_at, datetime.utcnow())
        finally:
            with SessionLocal() as db:
                db.query(BlastJob).filter(BlastJob.id == job_id).delete()
                account = db.get(TelegramAccount, self.owner_account_id)
                account.blast_available_at = None
                db.commit()

    def test_sheet_job_automatically_includes_new_eligible_accounts(self):
        with SessionLocal() as db:
            new_account = TelegramAccount(
                user_id=self.owner_id,
                label="New Sheet Account",
                phone="+629811223344",
                session_str="new-sheet-session",
                api_id=88,
                api_hash="new-sheet-hash",
                is_active=1,
            )
            db.add(new_account)
            db.flush()
            job = BlastJob(
                user_id=self.owner_id,
                source="sheet",
                status="queued",
                message="dynamic account test",
                accounts_json=json.dumps([self.owner_account_id]),
            )
            db.add(job)
            db.flush()
            self.assertIn(
                new_account.id,
                blast_manager._available_account_ids(
                    db, job, [self.owner_account_id]
                ),
            )
            job.source = "manual"
            self.assertEqual(
                blast_manager._available_account_ids(
                    db, job, [self.owner_account_id]
                ),
                [self.owner_account_id],
            )
            blast_manager.refresh_account_pool(self.owner_id)
            self.assertTrue(asyncio.run(
                blast_manager._wait_for_account_pool_change(self.owner_id, 3600)
            ))
            blast_manager.account_pool_events.pop(self.owner_id, None)
            db.rollback()

    def test_sheet_worker_repeats_immediately_after_changes(self):
        worker = SheetBlaster()
        worker.sync_once = AsyncMock(return_value=True)
        client_context = AsyncMock()
        client_context.__aenter__.return_value = object()

        async def exercise():
            with (
                patch("app.services.sheet_blaster.httpx.AsyncClient", return_value=client_context),
                patch("app.services.sheet_blaster.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)) as sleep,
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await worker._run()
                sleep.assert_awaited_once_with(1)

        asyncio.run(exercise())

    def test_sheet_interval_updates_a_running_job(self):
        with SessionLocal() as db:
            job = BlastJob(
                user_id=self.owner_id,
                source="sheet",
                status="running",
                message="live interval test",
                delay_seconds=3600,
                delay_max_seconds=3600,
                accounts_json=json.dumps([self.owner_account_id]),
            )
            db.add(job)
            db.commit()
            job_id = job.id

        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"interval": 12},
        )
        client = SimpleNamespace(post=AsyncMock(return_value=response))
        worker = SheetBlaster()
        blast_manager.account_pool_events.pop(self.owner_id, None)
        try:
            with SessionLocal() as db:
                job = db.get(BlastJob, job_id)
                self.assertTrue(asyncio.run(worker._sync_settings(client, db, job)))
                self.assertEqual((job.delay_seconds, job.delay_max_seconds), (12, 12))
            self.assertTrue(blast_manager.account_pool_events[self.owner_id].is_set())
        finally:
            blast_manager.account_pool_events.pop(self.owner_id, None)
            with SessionLocal() as db:
                db.query(BlastJob).filter(BlastJob.id == job_id).delete()
                db.commit()

    def test_sheet_waits_the_configured_minutes_between_jobs(self):
        now = datetime.utcnow()
        self.assertTrue(SheetBlaster._waiting_for_next_job(
            now - timedelta(minutes=9), 10, now
        ))
        self.assertFalse(SheetBlaster._waiting_for_next_job(
            now - timedelta(minutes=10), 10, now
        ))
        self.assertEqual(SheetBlaster._job_delay_minutes({"job_delay_minutes": 10}), 10)

    def test_finished_job_with_failed_targets_is_still_completed(self):
        with SessionLocal() as db:
            job = BlastJob(
                user_id=self.owner_id,
                status="running",
                message="completed with failed target",
                accounts_json=json.dumps([self.owner_account_id]),
                total_count=1,
            )
            db.add(job)
            db.flush()
            db.add(BlastRecipient(
                job_id=job.id,
                account_id=self.owner_account_id,
                username="missing_target",
                normalized_username="missing_target",
                status="failed",
                error="Username tidak ditemukan",
            ))
            db.commit()
            job_id = job.id

        try:
            blast_manager._finalize_job(job_id)
            with SessionLocal() as db:
                self.assertEqual(db.get(BlastJob, job_id).status, "completed")
        finally:
            with SessionLocal() as db:
                db.query(BlastJob).filter(BlastJob.id == job_id).delete()
                db.commit()

    def test_completed_recipient_updates_job_counts_without_full_recount(self):
        with SessionLocal() as db:
            job = BlastJob(
                user_id=self.owner_id,
                status="running",
                message="count test",
                accounts_json=json.dumps([self.owner_account_id]),
                total_count=1,
                pending_count=1,
            )
            db.add(job)
            db.flush()
            recipient = BlastRecipient(
                job_id=job.id,
                account_id=self.owner_account_id,
                username="count_target",
                normalized_username="count_target",
            )
            db.add(recipient)
            db.commit()
            job_id = job.id
            recipient_id = recipient.id

        sender = SimpleNamespace(send_message=AsyncMock())
        try:
            self.assertEqual(
                asyncio.run(blast_manager._send_one(sender, job_id, recipient_id, "test", None)),
                "continue",
            )
            with SessionLocal() as db:
                job = db.get(BlastJob, job_id)
                account = db.get(TelegramAccount, self.owner_account_id)
                self.assertEqual((job.sent_count, job.pending_count), (1, 0))
                self.assertIsNotNone(account.last_blast_sent_at)
        finally:
            with SessionLocal() as db:
                db.query(BlastJob).filter(BlastJob.id == job_id).delete()
                account = db.get(TelegramAccount, self.owner_account_id)
                account.last_blast_sent_at = None
                db.commit()

    def test_header_does_not_render_profile_summary(self):
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('class="user-area"', response.text)
        self.assertNotIn('class="user-chip"', response.text)
        self.assertNotIn('action="/logout"', response.text)

    def test_telegram_secrets_are_encrypted_at_rest(self):
        with SessionLocal() as db:
            account = db.get(TelegramAccount, self.owner_account_id)
            self.assertEqual(account.session_str, "owner-session")
            self.assertEqual(account.api_hash, "owner-hash")
        with sqlite3.connect(TEST_DB) as connection:
            session_str, api_hash = connection.execute(
                "SELECT session_str, api_hash FROM telegram_accounts WHERE id = ?",
                (self.owner_account_id,),
            ).fetchone()
        self.assertTrue(session_str.startswith(ENCRYPTED_PREFIX))
        self.assertTrue(api_hash.startswith(ENCRYPTED_PREFIX))
        self.assertNotIn("owner-session", session_str)
        self.assertNotIn("owner-hash", api_hash)

    def test_encryption_key_can_be_rotated(self):
        original_key = os.environ["DATA_ENCRYPTION_KEY"]
        original_old = os.environ.get("DATA_ENCRYPTION_KEY_OLD")
        try:
            os.environ["DATA_ENCRYPTION_KEY"] = "old-key"
            old_ciphertext = encrypt_sensitive("rotation-secret")
            os.environ["DATA_ENCRYPTION_KEY"] = "new-key"
            os.environ["DATA_ENCRYPTION_KEY_OLD"] = "old-key"
            rotated = encrypt_sensitive(old_ciphertext)
            self.assertNotEqual(rotated, old_ciphertext)
            self.assertEqual(decrypt_sensitive(rotated), "rotation-secret")
        finally:
            os.environ["DATA_ENCRYPTION_KEY"] = original_key
            if original_old is None:
                os.environ.pop("DATA_ENCRYPTION_KEY_OLD", None)
            else:
                os.environ["DATA_ENCRYPTION_KEY_OLD"] = original_old

    def test_other_users_job_is_not_addressable(self):
        response = self.client.get(f"/api/jobs/{self.other_job_id}")
        self.assertEqual(response.status_code, 404)

    def test_other_users_account_cannot_be_deleted(self):
        dashboard = self.client.get("/dashboard")
        response = self.client.post(
            f"/delete-account/{self.other_account_id}",
            data={"csrf_token": _csrf_from(dashboard.text)},
        )
        self.assertEqual(response.status_code, 404)
        with SessionLocal() as db:
            self.assertIsNotNone(db.get(TelegramAccount, self.other_account_id))

    def test_inbox_only_renders_current_users_messages(self):
        with SessionLocal() as db:
            db.add_all([
                InboxConversation(
                    user_id=self.owner_id,
                    account_id=self.owner_account_id,
                    peer_id=101,
                    peer_access_hash=1001,
                    peer_name="Owner Customer",
                    peer_username="owner_customer",
                ),
                InboxConversation(
                    user_id=self.other_id,
                    account_id=self.other_account_id,
                    peer_id=202,
                    peer_access_hash=2002,
                    peer_name="Other Secret Customer",
                    peer_username="other_secret",
                ),
                InboxMessage(
                    user_id=self.owner_id,
                    account_id=self.owner_account_id,
                    peer_id=101,
                    peer_access_hash=1001,
                    peer_name="Owner Customer",
                    peer_username="owner_customer",
                    telegram_message_id=1,
                    direction="in",
                    body="Owner reply",
                ),
                InboxMessage(
                    user_id=self.other_id,
                    account_id=self.other_account_id,
                    peer_id=202,
                    peer_access_hash=2002,
                    peer_name="Other Secret Customer",
                    peer_username="other_secret",
                    telegram_message_id=1,
                    direction="in",
                    body="Other secret reply",
                ),
            ])
            db.commit()

        response = self.client.get("/inbox")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Owner Customer", response.text)
        self.assertIn(f'data-account-status="{self.owner_account_id}"', response.text)
        self.assertNotIn("Other Secret Customer", response.text)
        self.assertNotIn("Other secret reply", response.text)

    def test_inbox_conversation_can_be_archived_restored_and_deleted(self):
        peer_id = 606
        with SessionLocal() as db:
            db.add_all([
                InboxConversation(
                    user_id=self.owner_id,
                    account_id=self.owner_account_id,
                    peer_id=peer_id,
                    peer_access_hash=6006,
                    peer_name="Archived Customer",
                ),
                InboxConversation(
                    user_id=self.other_id,
                    account_id=self.other_account_id,
                    peer_id=peer_id,
                    peer_access_hash=6007,
                    peer_name="Other Archived Customer",
                ),
                InboxMessage(
                    user_id=self.owner_id,
                    account_id=self.owner_account_id,
                    peer_id=peer_id,
                    peer_access_hash=6006,
                    peer_name="Archived Customer",
                    telegram_message_id=1,
                    direction="in",
                    body="Archive this",
                ),
                InboxMessage(
                    user_id=self.other_id,
                    account_id=self.other_account_id,
                    peer_id=peer_id,
                    peer_access_hash=6007,
                    peer_name="Other Archived Customer",
                    telegram_message_id=1,
                    direction="in",
                    body="Keep this",
                ),
            ])
            db.commit()

        inbox = self.client.get(f"/inbox?account_id={self.owner_account_id}&peer_id={peer_id}")
        form = {
            "csrf_token": _csrf_from(inbox.text),
            "account_id": self.owner_account_id,
            "peer_id": peer_id,
            "archived": "false",
        }
        forbidden = self.client.post(
            "/inbox/conversation/archive",
            data={**form, "account_id": self.other_account_id},
        )
        self.assertEqual(forbidden.status_code, 404)
        archived = self.client.post(
            "/inbox/conversation/archive", data=form, follow_redirects=False
        )
        self.assertEqual(archived.status_code, 303)
        self.assertNotIn("Archived Customer", self.client.get("/inbox").text)
        archive_page = self.client.get("/inbox?archived=true")
        self.assertIn("Archived Customer", archive_page.text)
        self.assertNotIn("Other Archived Customer", archive_page.text)

        form["archived"] = "true"
        restored = self.client.post(
            "/inbox/conversation/restore", data=form, follow_redirects=False
        )
        self.assertEqual(restored.status_code, 303)
        self.assertIn("Archived Customer", self.client.get("/inbox").text)

        form["archived"] = "false"
        deleted = self.client.post(
            "/inbox/conversation/delete", data=form, follow_redirects=False
        )
        self.assertEqual(deleted.status_code, 303)
        with SessionLocal() as db:
            self.assertEqual(db.query(InboxMessage).filter(
                InboxMessage.user_id == self.owner_id,
                InboxMessage.peer_id == peer_id,
            ).count(), 0)
            self.assertEqual(db.query(InboxMessage).filter(
                InboxMessage.user_id == self.other_id,
                InboxMessage.peer_id == peer_id,
            ).count(), 1)
            db.query(InboxMessage).filter(
                InboxMessage.user_id == self.other_id,
                InboxMessage.peer_id == peer_id,
            ).delete(synchronize_session=False)
            db.query(InboxConversation).filter(
                InboxConversation.user_id == self.other_id,
                InboxConversation.peer_id == peer_id,
            ).delete(synchronize_session=False)
            db.commit()

    def test_whatsapp_style_inbox_actions_and_bulk_flow(self):
        peer_id = 909
        bulk_peer_id = 910
        with SessionLocal() as db:
            for current_peer, name in ((peer_id, "Action Customer"), (bulk_peer_id, "Bulk Customer")):
                db.add(InboxConversation(
                    user_id=self.owner_id,
                    account_id=self.owner_account_id,
                    peer_id=current_peer,
                    peer_access_hash=current_peer * 10,
                    peer_name=name,
                ))
                db.add(InboxMessage(
                    user_id=self.owner_id,
                    account_id=self.owner_account_id,
                    peer_id=current_peer,
                    peer_access_hash=current_peer * 10,
                    peer_name=name,
                    telegram_message_id=1,
                    direction="in",
                    body=f"Message for {name}",
                ))
            db.commit()

        inbox = self.client.get(f"/inbox?account_id={self.owner_account_id}&peer_id={peer_id}")
        self.assertEqual(inbox.status_code, 200)
        for text in (
            "Pesan berbintang", "Pilih obrolan", "Tandai semua sudah dibaca",
            "Bisukan notifikasi", "Sematkan chat", "Tambah ke favorit",
            "Tambahkan ke daftar", "Blokir kontak", "Bersihkan obrolan",
            "Hapus obrolan", 'id="attachment-button"', 'id="emoji-button"',
            'id="record-button"',
        ):
            self.assertIn(text, inbox.text)
        self.assertRegex(inbox.text, r'class="menu-action-icon">\s*<svg')
        self.assertIn('title="Tandai sudah dibaca" aria-label="Tandai sudah dibaca"', inbox.text)
        self.assertIn("checkbox.checked = !checkbox.checked", inbox.text)
        self.assertRegex(inbox.text, r'id="emoji-button"[^>]*><svg')
        self.assertNotIn("Grup baru", inbox.text)
        csrf = _csrf_from(inbox.text)
        form = {
            "csrf_token": csrf,
            "account_id": self.owner_account_id,
            "peer_id": peer_id,
            "view": "all",
        }

        for action in ("mute", "pin", "favorite"):
            response = self.client.post(
                f"/inbox/conversation/{action}", data=form, follow_redirects=False
            )
            self.assertEqual(response.status_code, 303)
        self.client.post("/inbox/conversation/list", data={**form, "list_label": "Prioritas"})
        unread = self.client.post(
            "/inbox/conversation/unread", data=form, follow_redirects=False
        )
        self.assertNotIn("account_id", unread.headers["location"])

        with patch(
            "app.routers.inbox.inbox_manager.set_blocked_all",
            AsyncMock(return_value=({self.owner_account_id}, 1)),
        ) as blocker:
            response = self.client.post("/inbox/conversation/block", data=form)
        self.assertEqual(response.status_code, 200)
        blocker.assert_awaited_once_with(self.owner_id, peer_id, None, True)

        selected = self.client.get(
            f"/inbox?account_id={self.owner_account_id}&peer_id={peer_id}"
        )
        message_id = re.search(r'/inbox/message/(\d+)/star', selected.text).group(1)
        starred = self.client.post(
            f"/inbox/message/{message_id}/star", data=form, follow_redirects=False
        )
        self.assertEqual(starred.status_code, 303)
        self.assertIn("Action Customer", self.client.get("/inbox?view=starred").text)

        clear = self.client.post(
            "/inbox/conversation/clear", data=form, follow_redirects=False
        )
        self.assertEqual(clear.status_code, 303)
        with SessionLocal() as db:
            state = db.query(InboxConversation).filter(
                InboxConversation.user_id == self.owner_id,
                InboxConversation.peer_id == peer_id,
            ).one()
            self.assertTrue(state.is_muted and state.is_pinned and state.is_favorite and state.is_blocked)
            self.assertEqual(state.list_label, "Prioritas")
            self.assertEqual(db.query(InboxMessage).filter(
                InboxMessage.user_id == self.owner_id,
                InboxMessage.peer_id == peer_id,
            ).count(), 0)

        bulk_form = {
            "csrf_token": csrf,
            "view": "all",
            "conversation_keys": [
                f"{self.owner_account_id}:{bulk_peer_id}",
                f"{self.other_account_id}:202",
            ],
        }
        for action in ("read", "mute", "archive", "delete"):
            response = self.client.post(
                "/inbox/conversations/bulk",
                data={**bulk_form, "action": action},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)
        with SessionLocal() as db:
            self.assertIsNone(db.query(InboxConversation).filter(
                InboxConversation.user_id == self.owner_id,
                InboxConversation.peer_id == bulk_peer_id,
            ).first())
            self.assertIsNotNone(db.query(InboxConversation).filter(
                InboxConversation.user_id == self.other_id,
                InboxConversation.peer_id == 202,
            ).first())
            db.query(InboxConversation).filter(
                InboxConversation.user_id == self.owner_id,
                InboxConversation.peer_id == peer_id,
            ).delete(synchronize_session=False)
            db.commit()

    def test_block_and_unblock_contact_use_every_connected_owner_account(self):
        peer_id = 9292
        with SessionLocal() as db:
            second_account = TelegramAccount(
                user_id=self.owner_id,
                label="Second Block Account",
                phone="+629292929292",
                session_str="second-block-session",
                api_id=92,
                api_hash="second-block-hash",
                is_active=1,
            )
            db.add(second_account)
            db.flush()
            db.add(InboxConversation(
                user_id=self.owner_id,
                account_id=self.owner_account_id,
                peer_id=peer_id,
                peer_access_hash=929292,
                peer_name="Shared Block Contact",
                peer_username="shared_block_contact",
            ))
            db.commit()
            second_account_id = second_account.id

        class FakeClient:
            def __init__(self, access_hash):
                self.access_hash = access_hash
                self.requests = []
                self.resolved = []

            def is_connected(self):
                return True

            async def get_input_entity(self, username):
                self.resolved.append(username)
                return types.InputPeerUser(peer_id, self.access_hash)

            async def __call__(self, request):
                self.requests.append(request)

        first_client = FakeClient(929292)
        second_client = FakeClient(929293)
        other_client = FakeClient(929294)
        try:
            with patch.object(inbox_manager, "clients", {
                self.owner_account_id: first_client,
                second_account_id: second_client,
                self.other_account_id: other_client,
            }):
                blocked_ids, blocked_total = asyncio.run(
                    inbox_manager.set_blocked_all(
                        self.owner_id, peer_id, "shared_block_contact", True
                    )
                )
                unblocked_ids, unblocked_total = asyncio.run(
                    inbox_manager.set_blocked_all(
                        self.owner_id, peer_id, "shared_block_contact", False
                    )
                )
            self.assertEqual(blocked_ids, {self.owner_account_id, second_account_id})
            self.assertEqual(unblocked_ids, blocked_ids)
            self.assertEqual((blocked_total, unblocked_total), (2, 2))
            self.assertEqual(second_client.resolved, ["shared_block_contact", "shared_block_contact"])
            self.assertEqual(
                [type(request).__name__ for request in first_client.requests],
                ["BlockRequest", "UnblockRequest"],
            )
            self.assertEqual(len(second_client.requests), 2)
            self.assertEqual(other_client.requests, [])
        finally:
            with SessionLocal() as db:
                db.query(InboxConversation).filter(
                    InboxConversation.user_id == self.owner_id,
                    InboxConversation.peer_id == peer_id,
                ).delete(synchronize_session=False)
                db.query(TelegramAccount).filter(
                    TelegramAccount.id == second_account_id
                ).delete(synchronize_session=False)
                db.commit()

    def test_inbox_listener_keeps_archived_conversation_silent(self):
        with SessionLocal() as db:
            db.add_all([
                InboxConversation(
                    user_id=self.owner_id,
                    account_id=self.owner_account_id,
                    peer_id=707,
                    peer_access_hash=7007,
                    peer_name="Pelanggan Lama",
                    is_archived=True,
                ),
                InboxMessage(
                    user_id=self.owner_id,
                    account_id=self.owner_account_id,
                    peer_id=707,
                    peer_access_hash=7007,
                    peer_name="Pelanggan Lama",
                    telegram_message_id=76,
                    direction="in",
                    body="Pesan terarsip",
                    is_archived=True,
                ),
            ])
            db.commit()

        class FakeEvent:
            is_private = True
            raw_text = "Balasan masuk untuk Meysa"
            message = SimpleNamespace(id=77, date=datetime.utcnow(), media=None)

            async def get_input_chat(self):
                return types.InputPeerUser(707, 7007)

            async def get_chat(self):
                return SimpleNamespace(
                    first_name="Pelanggan",
                    last_name="Baru",
                    username="pelanggan_baru",
                )

        asyncio.run(inbox_manager._store_incoming(self.owner_account_id, FakeEvent()))
        with SessionLocal() as db:
            messages = db.query(InboxMessage).filter(
                InboxMessage.account_id == self.owner_account_id,
                InboxMessage.peer_id == 707,
            ).all()
            self.assertEqual(len(messages), 2)
            self.assertTrue(messages[0].is_archived)
            self.assertIn("Balasan masuk untuk Meysa", {message.body for message in messages})
            self.assertTrue(db.query(InboxConversation).filter(
                InboxConversation.account_id == self.owner_account_id,
                InboxConversation.peer_id == 707,
            ).one().is_archived)

        unread = self.client.get("/api/inbox/unread").json()
        self.assertNotEqual(unread["latest_preview"], "Balasan masuk untuk Meysa")

    def test_permanent_session_error_stops_listener_and_marks_account_inactive(self):
        client = SimpleNamespace(
            connect=AsyncMock(side_effect=AuthKeyDuplicatedError(request=None)),
            add_event_handler=lambda *_args: None,
            is_connected=lambda: False,
        )
        with (
            patch("app.services.inbox_manager.StringSession", return_value=object()),
            patch("app.services.inbox_manager.TelegramClient", return_value=client),
        ):
            asyncio.run(inbox_manager._listen(self.owner_account_id))
        with SessionLocal() as db:
            account = db.get(TelegramAccount, self.owner_account_id)
            self.assertFalse(account.is_active)
            account.is_active = 1
            db.commit()

    def test_inbox_reply_automatically_uses_receiving_account(self):
        peer_id = 303
        access_hash = 3003
        with SessionLocal() as db:
            db.add_all([
                InboxConversation(
                    user_id=self.owner_id,
                    account_id=self.owner_account_id,
                    peer_id=peer_id,
                    peer_access_hash=access_hash,
                    peer_name="Meysa Customer",
                    peer_username="meysa_customer",
                ),
                InboxMessage(
                    user_id=self.owner_id,
                    account_id=self.owner_account_id,
                    peer_id=peer_id,
                    peer_access_hash=access_hash,
                    peer_name="Meysa Customer",
                    peer_username="meysa_customer",
                    telegram_message_id=1,
                    direction="in",
                    body="Saya tertarik",
                ),
            ])
            db.commit()

        inbox = self.client.get(
            f"/inbox?account_id={self.owner_account_id}&peer_id={peer_id}&view=unread"
        )
        sender = AsyncMock(return_value=(2, datetime.utcnow()))
        with patch("app.routers.inbox.inbox_manager.send_reply", sender):
            response = self.client.post(
                "/inbox/reply",
                data={
                    "csrf_token": _csrf_from(inbox.text),
                    "account_id": self.owner_account_id,
                    "peer_id": peer_id,
                    "view": "unread",
                    "body": "Baik, saya bantu.",
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/inbox?view=unread")
        sender.assert_awaited_once_with(
            self.owner_account_id,
            peer_id,
            access_hash,
            "Baik, saya bantu.",
        )
        with SessionLocal() as db:
            reply = db.query(InboxMessage).filter(
                InboxMessage.account_id == self.owner_account_id,
                InboxMessage.peer_id == peer_id,
                InboxMessage.direction == "out",
            ).one()
            self.assertEqual(reply.body, "Baik, saya bantu.")

        voice_sender = AsyncMock(return_value=(3, datetime.utcnow()))
        with patch("app.routers.inbox.inbox_manager.send_reply", voice_sender):
            response = self.client.post(
                "/inbox/reply",
                data={
                    "csrf_token": _csrf_from(inbox.text),
                    "account_id": self.owner_account_id,
                    "peer_id": peer_id,
                    "body": "",
                    "voice_note": "true",
                },
                files={"attachment": ("pesan.webm", b"voice-data", "audio/webm")},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303)
        self.assertTrue(voice_sender.await_args.kwargs["voice_note"])
        media_path = Path(voice_sender.await_args.kwargs["file_path"])
        self.assertTrue(media_path.is_file())
        with SessionLocal() as db:
            voice = db.query(InboxMessage).filter(
                InboxMessage.account_id == self.owner_account_id,
                InboxMessage.peer_id == peer_id,
                InboxMessage.telegram_message_id == 3,
            ).one()
            self.assertEqual(voice.media_type, "audio/webm")
            db.delete(voice)
            db.commit()
        media_path.unlink(missing_ok=True)

    def test_inbox_routes_a_second_receiving_account(self):
        with SessionLocal() as db:
            account = TelegramAccount(
                user_id=self.owner_id,
                label="Second Account",
                phone="+620000000003",
                session_str="second-session",
                api_id=3,
                api_hash="second-hash",
                is_active=1,
            )
            db.add(account)
            db.commit()
            db.refresh(account)
            second_account_id = account.id

        class FakeEvent:
            is_private = True
            raw_text = "Masuk lewat akun kedua"
            message = SimpleNamespace(id=88, date=datetime.utcnow(), media=None)

            async def get_input_chat(self):
                return types.InputPeerUser(808, 8008)

            async def get_chat(self):
                return SimpleNamespace(first_name="Pelanggan Kedua", last_name=None, username=None)

        asyncio.run(inbox_manager._store_incoming(second_account_id, FakeEvent()))
        inbox = self.client.get(f"/inbox?account_id={second_account_id}&peer_id=808")
        sender = AsyncMock(return_value=(89, datetime.utcnow()))
        with patch("app.routers.inbox.inbox_manager.send_reply", sender):
            response = self.client.post(
                "/inbox/reply",
                data={
                    "csrf_token": _csrf_from(inbox.text),
                    "account_id": second_account_id,
                    "peer_id": 808,
                    "body": "Balasan akun kedua",
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        sender.assert_awaited_once_with(second_account_id, 808, 8008, "Balasan akun kedua")

    def test_special_auto_reply_is_configurable_and_scoped_per_account(self):
        page = self.client.get("/special")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Fitur Spesial", page.text)
        self.assertIn('name="mode"', page.text)
        self.assertIn('value="always"', page.text)
        response = self.client.post(
            "/special/auto-reply",
            data={
                "csrf_token": _csrf_from(page.text),
                "enabled": "true",
                "message": "Halo, pesanmu sudah kami terima.",
                "mode": "cooldown",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

        with SessionLocal() as db:
            owner = db.get(User, self.owner_id)
            self.assertTrue(owner.auto_reply_enabled)
            self.assertEqual(owner.auto_reply_mode, "cooldown")
            second_account = TelegramAccount(
                user_id=self.owner_id,
                label="Auto Reply Account B",
                phone="+620000000004",
                session_str="auto-reply-session",
                api_id=4,
                api_hash="auto-reply-hash",
                is_active=1,
            )
            db.add(second_account)
            db.commit()
            db.refresh(second_account)
            second_account_id = second_account.id

        class FakeEvent:
            is_private = True
            raw_text = "Halo"

            def __init__(self, message_id):
                self.message = SimpleNamespace(
                    id=message_id,
                    date=datetime.utcnow(),
                    media=None,
                )

            async def get_input_chat(self):
                return types.InputPeerUser(909, 9009)

            async def get_chat(self):
                return SimpleNamespace(first_name="Zelza", last_name=None, username="zelza")

        sender = AsyncMock(side_effect=[
            (901, datetime.utcnow()),
            (902, datetime.utcnow()),
            (903, datetime.utcnow()),
        ])
        try:
            with patch.object(inbox_manager, "send_reply", sender):
                asyncio.run(inbox_manager._store_incoming(self.owner_account_id, FakeEvent(1)))
                asyncio.run(inbox_manager._store_incoming(self.owner_account_id, FakeEvent(2)))
                asyncio.run(inbox_manager._store_incoming(second_account_id, FakeEvent(1)))
                with SessionLocal() as db:
                    conversation = db.query(InboxConversation).filter(
                        InboxConversation.account_id == self.owner_account_id,
                        InboxConversation.peer_id == 909,
                    ).one()
                    conversation.auto_replied_at = datetime.utcnow() - timedelta(hours=25)
                    db.commit()
                asyncio.run(inbox_manager._store_incoming(self.owner_account_id, FakeEvent(3)))

            self.assertEqual([call.args[0] for call in sender.await_args_list], [
                self.owner_account_id,
                second_account_id,
                self.owner_account_id,
            ])
            with SessionLocal() as db:
                conversations = db.query(InboxConversation).filter(
                    InboxConversation.user_id == self.owner_id,
                    InboxConversation.peer_id == 909,
                ).all()
                self.assertEqual(len(conversations), 2)
                self.assertTrue(all(item.auto_replied_at for item in conversations))
                self.assertEqual(db.query(InboxMessage).filter(
                    InboxMessage.user_id == self.owner_id,
                    InboxMessage.peer_id == 909,
                    InboxMessage.direction == "out",
                ).count(), 3)
        finally:
            with SessionLocal() as db:
                db.query(InboxMessage).filter(
                    InboxMessage.user_id == self.owner_id,
                    InboxMessage.peer_id == 909,
                ).delete(synchronize_session=False)
                db.query(InboxConversation).filter(
                    InboxConversation.user_id == self.owner_id,
                    InboxConversation.peer_id == 909,
                ).delete(synchronize_session=False)
                db.query(TelegramAccount).filter(
                    TelegramAccount.id == second_account_id,
                ).delete(synchronize_session=False)
                owner = db.get(User, self.owner_id)
                owner.auto_reply_enabled = False
                owner.auto_reply_message = None
                owner.auto_reply_mode = "cooldown"
                db.commit()

    def test_special_auto_reply_always_mode_replies_to_every_message(self):
        peer_id = 9191
        with SessionLocal() as db:
            owner = db.get(User, self.owner_id)
            owner.auto_reply_enabled = True
            owner.auto_reply_message = "Balasan setiap pesan"
            owner.auto_reply_mode = "always"
            db.add(InboxConversation(
                user_id=self.owner_id,
                account_id=self.owner_account_id,
                peer_id=peer_id,
                peer_access_hash=1919,
                peer_name="Always Test",
            ))
            db.commit()

        try:
            first = inbox_manager._claim_auto_reply(self.owner_account_id, peer_id)
            second = inbox_manager._claim_auto_reply(self.owner_account_id, peer_id)
            self.assertEqual(first[0], "Balasan setiap pesan")
            self.assertEqual(second[0], "Balasan setiap pesan")
        finally:
            with SessionLocal() as db:
                db.query(InboxConversation).filter(
                    InboxConversation.account_id == self.owner_account_id,
                    InboxConversation.peer_id == peer_id,
                ).delete()
                owner = db.get(User, self.owner_id)
                owner.auto_reply_enabled = False
                owner.auto_reply_message = None
                owner.auto_reply_mode = "cooldown"
                db.commit()

    def test_inbox_startup_includes_all_connected_accounts(self):
        with (
            patch.dict(os.environ, {"INBOX_LISTENERS_ENABLED": "true", "INBOX_ACCOUNT_ID": ""}),
            patch.object(inbox_manager, "start") as starter,
        ):
            asyncio.run(inbox_manager.start_all())

        started = {call.args[0] for call in starter.call_args_list}
        self.assertTrue({self.owner_account_id, self.other_account_id}.issubset(started))

    def test_railway_ignores_local_inbox_account_filter(self):
        with patch.dict(os.environ, {
            "RAILWAY_ENVIRONMENT": "production",
            "INBOX_ACCOUNT_ID": str(self.owner_account_id),
        }):
            self.assertTrue(inbox_manager.account_enabled(self.other_account_id))

    def test_inbox_status_only_reports_current_users_connected_accounts(self):
        with patch.object(
            inbox_manager,
            "connected",
            side_effect=lambda account_id: account_id in {
                self.owner_account_id,
                self.other_account_id,
            },
        ):
            response = self.client.get("/api/inbox/unread")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["connected_account_ids"], [self.owner_account_id])

    def test_inbox_reply_cannot_use_another_users_account(self):
        dashboard = self.client.get("/dashboard")
        sender = AsyncMock()
        with patch("app.routers.inbox.inbox_manager.send_reply", sender):
            response = self.client.post(
                "/inbox/reply",
                data={
                    "csrf_token": _csrf_from(dashboard.text),
                    "account_id": self.other_account_id,
                    "peer_id": 202,
                    "body": "Tidak boleh terkirim",
                },
            )
        self.assertEqual(response.status_code, 404)
        sender.assert_not_awaited()

    def test_scraper_poll_is_scoped_to_its_owner(self):
        scraper._jobs["other-job"] = {
            "user_id": self.other_id,
            "status": "done",
            "contacts": {"@secret"},
            "log": ["secret"],
            "total": 1,
        }
        response = self.client.get("/api/scraper/other-job")
        self.assertEqual(response.status_code, 404)

    def test_whatsapp_routes_are_removed(self):
        response = self.client.get("/wa")
        self.assertEqual(response.status_code, 404)

    def test_new_google_user_is_created_active(self):
        with SessionLocal() as db:
            user = upsert_google_user(db, {
                "sub": "new-google-sub",
                "email": "new-google@example.com",
                "email_verified": True,
                "name": "New Google User",
            })
            self.assertTrue(user.is_active)
            self.assertEqual(user.role, "user")

    def test_bootstrap_owner_is_claimed_without_losing_existing_data(self):
        with SessionLocal() as db:
            owner = upsert_google_user(db, {
                "sub": "owner-real-google-sub",
                "email": "owner@example.com",
                "email_verified": True,
                "name": "Owner From Google",
            })
            account = db.get(TelegramAccount, self.owner_account_id)
            self.assertEqual(account.user_id, owner.id)
            self.assertEqual(owner.google_sub, "owner-real-google-sub")

    def test_unauthenticated_browser_is_sent_to_login(self):
        client = TestClient(app, follow_redirects=False)
        response = client.get("/dashboard", headers={"Accept": "text/html"})
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/login"))

    def test_basic_auth_is_not_accepted(self):
        client = TestClient(app, follow_redirects=False)
        credentials = base64.b64encode(b"admin:removed").decode("ascii")
        response = client.get(
            "/dashboard",
            headers={
                "Accept": "text/html",
                "Authorization": f"Basic {credentials}",
            },
        )
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/login"))
        self.assertNotIn("www-authenticate", response.headers)

    def test_login_uses_scheme_independent_static_assets(self):
        client = TestClient(app)
        response = client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/static/css/porslabs-app.css"', response.text)
        self.assertIn('src="/static/js/porslabs-app.js"', response.text)
        self.assertNotIn("http://testserver/static/", response.text)

    def test_local_registration_and_password_login(self):
        client = TestClient(app, follow_redirects=False)
        register_page = client.get("/register")
        response = client.post(
            "/register",
            data={
                "csrf_token": _csrf_from(register_page.text),
                "username": "new.member",
                "email": "local-member@example.com",
                "password": "SafePassword123!",
                "password_confirmation": "SafePassword123!",
            },
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/dashboard")

        with SessionLocal() as db:
            user = db.query(User).filter(User.username == "new.member").one()
            self.assertNotIn("SafePassword123!", user.password_hash)
            self.assertTrue(verify_password("SafePassword123!", user.password_hash))
            self.assertTrue(user.google_sub.startswith("local:"))

        second_client = TestClient(app, follow_redirects=False)
        login_page = second_client.get("/login")
        login = second_client.post(
            "/login",
            data={
                "csrf_token": _csrf_from(login_page.text),
                "identity": "new.member",
                "password": "SafePassword123!",
                "remember_me": "1",
                "next_path": "/dashboard",
            },
        )
        self.assertEqual(login.status_code, 303)
        self.assertEqual(login.headers["location"], "/dashboard")
        dashboard = second_client.get("/dashboard")
        self.assertEqual(dashboard.status_code, 200)

    def test_wrong_local_password_is_rejected(self):
        with SessionLocal() as db:
            user = User(
                google_sub="local:wrong-password-test",
                username="wrongpass",
                email="wrong-password@example.com",
                password_hash=hash_password("CorrectPassword123!"),
                name="Wrong Password Test",
            )
            db.add(user)
            db.commit()

        client = TestClient(app, follow_redirects=False)
        login_page = client.get("/login")
        response = client.post(
            "/login",
            data={
                "csrf_token": _csrf_from(login_page.text),
                "identity": "wrongpass",
                "password": "not-the-password",
                "next_path": "/dashboard",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Username atau password tidak sesuai", response.text)

    def test_password_reset_token_is_single_use(self):
        token = "single-use-password-reset-token"
        with SessionLocal() as db:
            user = User(
                google_sub="local:password-reset-test",
                username="resetmember",
                email="reset-member@example.com",
                password_hash=hash_password("OldPassword123!"),
                password_reset_token_hash=hashlib.sha256(token.encode()).hexdigest(),
                password_reset_expires_at=datetime.utcnow() + timedelta(minutes=30),
                name="Reset Member",
            )
            db.add(user)
            db.commit()
            user_id = user.id

        client = TestClient(app, follow_redirects=False)
        reset_page = client.get(f"/reset-password?token={token}")
        self.assertEqual(reset_page.status_code, 200)
        response = client.post(
            "/reset-password",
            data={
                "csrf_token": _csrf_from(reset_page.text),
                "token": token,
                "password": "NewPassword123!",
                "password_confirmation": "NewPassword123!",
            },
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login?notice=password_reset")

        with SessionLocal() as db:
            user = db.get(User, user_id)
            self.assertTrue(verify_password("NewPassword123!", user.password_hash))
            self.assertIsNone(user.password_reset_token_hash)

        reused = client.get(f"/reset-password?token={token}")
        self.assertIn("tidak valid atau sudah kedaluwarsa", reused.text)

    def test_forgot_password_is_honest_when_email_is_not_configured(self):
        client = TestClient(app)
        response = client.get("/forgot-password")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Layanan email pemulihan belum diaktifkan", response.text)
        self.assertIn("disabled", response.text)

    def test_device_session_can_revoke_other_session(self):
        client = TestClient(app, follow_redirects=False)
        login_page = client.get("/login")
        with SessionLocal() as db:
            user = User(
                google_sub="local:device-session-test",
                username="devicemember",
                email="device-member@example.com",
                password_hash=hash_password("DevicePassword123!"),
                name="Device Member",
            )
            db.add(user)
            db.commit()
            user_id = user.id
        login = client.post(
            "/login",
            data={
                "csrf_token": _csrf_from(login_page.text),
                "identity": "devicemember",
                "password": "DevicePassword123!",
                "next_path": "/dashboard",
            },
        )
        self.assertEqual(login.status_code, 303)
        security_page = client.get("/security/sessions")
        self.assertEqual(security_page.status_code, 200)
        self.assertIn("Perangkat ini", security_page.text)

        with SessionLocal() as db:
            extra = DeviceSession(
                user_id=user_id,
                token_hash="f" * 64,
                device_name="Firefox · Linux",
                expires_at=datetime.utcnow() + timedelta(days=1),
            )
            db.add(extra)
            db.commit()
            extra_id = extra.id
        response = client.post(
            f"/security/sessions/{extra_id}/revoke",
            data={"csrf_token": _csrf_from(security_page.text)},
        )
        self.assertEqual(response.status_code, 303)
        with SessionLocal() as db:
            self.assertIsNotNone(db.get(DeviceSession, extra_id).revoked_at)


if __name__ == "__main__":
    unittest.main()
