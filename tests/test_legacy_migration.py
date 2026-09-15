import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest


class LegacyMigrationTests(unittest.TestCase):
    def test_existing_rows_are_assigned_to_bootstrap_owner_idempotently(self):
        db_path = Path("/tmp/porslabs-legacy-migration-test.db")
        db_path.unlink(missing_ok=True)

        with sqlite3.connect(db_path) as connection:
            connection.executescript(
                """
                CREATE TABLE telegram_accounts (
                    id INTEGER PRIMARY KEY,
                    label VARCHAR,
                    phone VARCHAR NOT NULL UNIQUE,
                    session_str VARCHAR,
                    api_id INTEGER NOT NULL,
                    api_hash VARCHAR NOT NULL,
                    is_active INTEGER,
                    created_at DATETIME
                );
                CREATE TABLE blast_jobs (
                    id INTEGER PRIMARY KEY,
                    status VARCHAR(20) NOT NULL,
                    message TEXT NOT NULL,
                    image_path VARCHAR,
                    delay_seconds FLOAT NOT NULL,
                    accounts_json TEXT NOT NULL,
                    consent_confirmed BOOLEAN NOT NULL,
                    total_count INTEGER NOT NULL,
                    sent_count INTEGER NOT NULL,
                    failed_count INTEGER NOT NULL,
                    skipped_count INTEGER NOT NULL,
                    pending_count INTEGER NOT NULL,
                    created_at DATETIME,
                    started_at DATETIME,
                    completed_at DATETIME
                );
                INSERT INTO telegram_accounts
                    (id, label, phone, session_str, api_id, api_hash, is_active)
                VALUES (1, 'Legacy Account', '+620000000009', 'legacy-session', 9, 'hash', 1);
                INSERT INTO blast_jobs
                    (id, status, message, delay_seconds, accounts_json, consent_confirmed,
                     total_count, sent_count, failed_count, skipped_count, pending_count)
                VALUES (1, 'completed', 'legacy job', 5, '[1]', 1, 1, 1, 0, 0, 0);
                """
            )

        env = os.environ.copy()
        env.update({
            "BLASTER_DB_PATH": str(db_path),
            "BOOTSTRAP_OWNER_EMAIL": "legacy-owner@example.com",
            "SESSION_SECRET": "legacy-migration-session-secret",
            "DATA_ENCRYPTION_KEY": "legacy-migration-encryption-key",
        })
        command = [sys.executable, "-c", "from app.main import app; print(app.title)"]
        for _ in range(2):
            result = subprocess.run(command, env=env, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)

        with sqlite3.connect(db_path) as connection:
            owner = connection.execute(
                "SELECT id, google_sub FROM users WHERE email = ?",
                ("legacy-owner@example.com",),
            ).fetchone()
            self.assertIsNotNone(owner)
            self.assertTrue(owner[1].startswith("bootstrap:"))
            self.assertEqual(
                connection.execute("SELECT user_id FROM telegram_accounts WHERE id = 1").fetchone()[0],
                owner[0],
            )
            self.assertEqual(
                connection.execute("SELECT user_id FROM blast_jobs WHERE id = 1").fetchone()[0],
                owner[0],
            )
            blast_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(blast_jobs)").fetchall()
            }
            self.assertIn("delay_max_seconds", blast_columns)
            user_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }
            self.assertTrue({
                "username",
                "password_hash",
                "password_reset_token_hash",
                "password_reset_expires_at",
            }.issubset(user_columns))


if __name__ == "__main__":
    unittest.main()
