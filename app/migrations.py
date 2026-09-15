import os
import sqlite3
from pathlib import Path

from sqlalchemy import Boolean, inspect, select, text

from app.database import Base, DB_PATH, IS_SQLITE, SessionLocal, engine
from app.models import InboxConversation, InboxMessage, User
from app.security import encrypt_sensitive, encryption_configured


def _bootstrap_email() -> str:
    return os.getenv("BOOTSTRAP_OWNER_EMAIL", "amelw778@gmail.com").strip().casefold()


def _sqlite_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        row[1]
        for row in connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    }


def _encrypt_sqlite_telegram_secrets(path: Path) -> int:
    """Encrypt plaintext secrets in the legacy SQLite file in one transaction."""
    if not path.exists():
        return 0
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "telegram_accounts" not in tables:
            return 0
        rows = connection.execute(
            "SELECT id, session_str, api_hash FROM telegram_accounts"
        ).fetchall()
        if rows and not encryption_configured():
            raise RuntimeError(
                "DATA_ENCRYPTION_KEY wajib diatur sebelum memigrasikan akun Telegram"
            )
        changed = 0
        for account_id, session_str, api_hash in rows:
            encrypted_session = encrypt_sensitive(session_str)
            encrypted_api_hash = encrypt_sensitive(api_hash)
            if encrypted_session != session_str or encrypted_api_hash != api_hash:
                connection.execute(
                    "UPDATE telegram_accounts SET session_str = ?, api_hash = ? WHERE id = ?",
                    (encrypted_session, encrypted_api_hash, account_id),
                )
                changed += 1
        connection.commit()
        return changed


def _upgrade_sqlite_schema() -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "users" in table_names:
        user_columns = {column["name"] for column in inspector.get_columns("users")}
        with engine.begin() as connection:
            if "username" not in user_columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN username VARCHAR(32)"))
            if "password_hash" not in user_columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN password_hash TEXT"))
            if "password_reset_token_hash" not in user_columns:
                connection.execute(
                    text("ALTER TABLE users ADD COLUMN password_reset_token_hash VARCHAR(64)")
                )
            if "password_reset_expires_at" not in user_columns:
                connection.execute(
                    text("ALTER TABLE users ADD COLUMN password_reset_expires_at DATETIME")
                )
            connection.execute(
                text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username)")
            )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_password_reset_token_hash "
                    "ON users (password_reset_token_hash)"
                )
            )

    with engine.begin() as connection:
        if "blast_jobs" in table_names:
            columns = {column["name"] for column in inspect(engine).get_columns("blast_jobs")}
            if "delay_max_seconds" not in columns:
                connection.execute(text("ALTER TABLE blast_jobs ADD COLUMN delay_max_seconds REAL"))
            if "user_id" not in columns:
                connection.execute(
                    text("ALTER TABLE blast_jobs ADD COLUMN user_id INTEGER REFERENCES users(id)")
                )

        if "telegram_accounts" in table_names:
            columns = {
                column["name"]
                for column in inspect(engine).get_columns("telegram_accounts")
            }
            if "user_id" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE telegram_accounts ADD COLUMN "
                        "user_id INTEGER REFERENCES users(id)"
                    )
                )


def _upgrade_inbox_schema() -> None:
    tables = set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        additions = {
            "users": {
                "auto_reply_enabled": "BOOLEAN NOT NULL DEFAULT FALSE",
                "auto_reply_message": "TEXT",
                "auto_reply_mode": "VARCHAR(20) NOT NULL DEFAULT 'cooldown'",
            },
            "inbox_conversations": {"auto_replied_at": "TIMESTAMP"},
            "inbox_messages": {
                "is_archived": "BOOLEAN NOT NULL DEFAULT FALSE",
                "is_starred": "BOOLEAN NOT NULL DEFAULT FALSE",
                "media_path": "VARCHAR(500)",
                "media_name": "VARCHAR(255)",
                "media_type": "VARCHAR(100)",
            },
        }
        for table, table_additions in additions.items():
            if table not in tables:
                continue
            columns = {column["name"] for column in inspect(engine).get_columns(table)}
            for name, definition in table_additions.items():
                if name not in columns:
                    connection.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                    ))


def _upgrade_sheet_blast_schema() -> None:
    tables = set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        if "blast_jobs" in tables:
            columns = {column["name"] for column in inspect(engine).get_columns("blast_jobs")}
            if "source" not in columns:
                connection.execute(text(
                    "ALTER TABLE blast_jobs ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT 'manual'"
                ))
            if "sheet_synced_at" not in columns:
                connection.execute(text("ALTER TABLE blast_jobs ADD COLUMN sheet_synced_at TIMESTAMP"))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_blast_jobs_source ON blast_jobs (source)"
            ))

        if "blast_recipients" in tables:
            columns = {
                column["name"] for column in inspect(engine).get_columns("blast_recipients")
            }
            if "sheet_item_id" not in columns:
                connection.execute(text(
                    "ALTER TABLE blast_recipients ADD COLUMN sheet_item_id VARCHAR(64)"
                ))
            if "sheet_synced_at" not in columns:
                connection.execute(text(
                    "ALTER TABLE blast_recipients ADD COLUMN sheet_synced_at TIMESTAMP"
                ))
            connection.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_blast_recipients_sheet_item_id "
                "ON blast_recipients (sheet_item_id)"
            ))

        if "telegram_accounts" in tables:
            columns = {
                column["name"] for column in inspect(engine).get_columns("telegram_accounts")
            }
            if "blast_available_at" not in columns:
                connection.execute(text(
                    "ALTER TABLE telegram_accounts ADD COLUMN blast_available_at TIMESTAMP"
                ))
            if "last_blast_sent_at" not in columns:
                connection.execute(text(
                    "ALTER TABLE telegram_accounts ADD COLUMN last_blast_sent_at TIMESTAMP"
                ))
            if "blast_recipients" in tables:
                connection.execute(text(
                    "UPDATE telegram_accounts SET last_blast_sent_at = ("
                    "SELECT MAX(sent_at) FROM blast_recipients "
                    "WHERE blast_recipients.account_id = telegram_accounts.id "
                    "AND blast_recipients.status = 'sent'"
                    ") WHERE last_blast_sent_at IS NULL"
                ))


def _backfill_inbox_conversations() -> None:
    with SessionLocal() as db:
        existing = {
            (row.account_id, row.peer_id)
            for row in db.query(InboxConversation.account_id, InboxConversation.peer_id)
        }
        for message in db.query(InboxMessage).order_by(
            InboxMessage.created_at.desc(), InboxMessage.id.desc()
        ):
            key = (message.account_id, message.peer_id)
            if key in existing:
                continue
            db.add(InboxConversation(
                user_id=message.user_id,
                account_id=message.account_id,
                peer_id=message.peer_id,
                peer_access_hash=message.peer_access_hash,
                peer_name=message.peer_name,
                peer_username=message.peer_username,
                is_archived=message.is_archived,
                created_at=message.created_at,
                updated_at=message.created_at,
            ))
            existing.add(key)
        db.commit()


def _copy_sqlite_to_postgres(source_path: Path) -> bool:
    """Copy a legacy SQLite database into an empty PostgreSQL schema once."""
    if not source_path.exists() or IS_SQLITE:
        return False

    users_table = Base.metadata.tables["users"]
    with engine.connect() as connection:
        if connection.execute(select(users_table.c.id).limit(1)).first():
            return False

    _encrypt_sqlite_telegram_secrets(source_path)
    table_order = ["users", "telegram_accounts", "blast_jobs", "blast_recipients"]
    with sqlite3.connect(source_path) as source:
        source.row_factory = sqlite3.Row
        source_tables = {
            row[0]
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        with engine.begin() as target:
            for table_name in table_order:
                if table_name not in source_tables:
                    continue
                table = Base.metadata.tables[table_name]
                target_columns = {column.name: column for column in table.columns}
                source_columns = _sqlite_columns(source, table_name)
                selected = [name for name in target_columns if name in source_columns]
                if not selected:
                    continue
                quoted = ", ".join(f'"{name}"' for name in selected)
                rows = []
                for source_row in source.execute(
                    f'SELECT {quoted} FROM "{table_name}"'
                ).fetchall():
                    values = dict(source_row)
                    for name, value in tuple(values.items()):
                        if isinstance(target_columns[name].type, Boolean) and value is not None:
                            values[name] = bool(value)
                    rows.append(values)
                if rows:
                    target.execute(table.insert(), rows)

            for table_name in table_order:
                table = Base.metadata.tables[table_name]
                if "id" not in table.c:
                    continue
                target.execute(
                    text(
                        "SELECT setval(pg_get_serial_sequence(:table_name, 'id'), "
                        "COALESCE((SELECT MAX(id) FROM " + table_name + "), 1), "
                        "(SELECT COUNT(*) > 0 FROM " + table_name + "))"
                    ),
                    {"table_name": table_name},
                )

    marker = source_path.with_name("sqlite-migrated-to-postgres.marker")
    marker.write_text("Migrated successfully; retain blaster.db as recovery archive.\n")
    return True


def _bootstrap_and_claim_legacy_rows() -> None:
    with SessionLocal() as db:
        email = _bootstrap_email()
        owner = db.query(User).filter(User.email == email).first()
        if not owner:
            owner = User(
                google_sub=f"bootstrap:{email}",
                email=email,
                name="PorsLabs Admin",
                role="admin",
            )
            db.add(owner)
            db.commit()
            db.refresh(owner)
        owner_id = owner.id

    with engine.begin() as connection:
        tables = set(inspect(engine).get_table_names())
        if "blast_jobs" in tables:
            connection.execute(
                text("UPDATE blast_jobs SET user_id = :owner_id WHERE user_id IS NULL"),
                {"owner_id": owner_id},
            )
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_blast_jobs_user_id ON blast_jobs (user_id)")
            )
        if "telegram_accounts" in tables:
            connection.execute(
                text("UPDATE telegram_accounts SET user_id = :owner_id WHERE user_id IS NULL"),
                {"owner_id": owner_id},
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_telegram_accounts_user_id "
                    "ON telegram_accounts (user_id)"
                )
            )


def initialize_database() -> None:
    """Create schema, migrate SQLite data once, and encrypt all Telegram secrets."""
    Base.metadata.create_all(bind=engine)
    if IS_SQLITE:
        _upgrade_sqlite_schema()
        Base.metadata.create_all(bind=engine)
        _encrypt_sqlite_telegram_secrets(DB_PATH)
    else:
        _copy_sqlite_to_postgres(DB_PATH)
    _upgrade_sheet_blast_schema()
    _upgrade_inbox_schema()
    _backfill_inbox_conversations()
    _bootstrap_and_claim_legacy_rows()
