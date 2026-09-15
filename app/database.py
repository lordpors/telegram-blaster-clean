import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker


def _data_directory() -> Path:
    volume_path = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    return Path(volume_path).expanduser().resolve() if volume_path else Path.cwd().resolve()


def _resolve_sqlite_path() -> Path:
    explicit_path = os.getenv("BLASTER_DB_PATH")
    if explicit_path:
        return Path(explicit_path).expanduser().resolve()

    return _data_directory() / "blaster.db"


DB_PATH = _resolve_sqlite_path()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgres://"):]
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgresql://"):]

SQLALCHEMY_DATABASE_URL = DATABASE_URL or f"sqlite:///{DB_PATH}"
IS_SQLITE = SQLALCHEMY_DATABASE_URL.startswith("sqlite:")

engine_options = {"pool_pre_ping": True}
if IS_SQLITE:
    engine_options["connect_args"] = {"check_same_thread": False, "timeout": 30}
else:
    engine_options.update({
        "pool_size": int(os.getenv("DB_POOL_SIZE", "5")),
        "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "5")),
        "pool_recycle": 300,
    })

engine = create_engine(SQLALCHEMY_DATABASE_URL, **engine_options)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _connection_record):
    if not IS_SQLITE:
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA cache_size=-20000")
    cursor.execute("PRAGMA temp_store=MEMORY")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
