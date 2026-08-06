from sqlalchemy import Column, Integer, String, DateTime
from datetime import datetime
from app.database import Base

class TelegramAccount(Base):
    __tablename__ = "telegram_accounts"

    id          = Column(Integer, primary_key=True, index=True)
    label       = Column(String, nullable=True)          # nama/label akun
    phone       = Column(String, unique=True, nullable=False)
    session_str = Column(String, nullable=True)          # session Telethon (permanen)
    api_id      = Column(Integer, nullable=False)
    api_hash    = Column(String, nullable=False)
    is_active   = Column(Integer, default=0)             # 1 = akun yang sedang dipakai
    created_at  = Column(DateTime, default=datetime.utcnow)
