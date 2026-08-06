from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from app.database import Base


class WAAccount(Base):
    __tablename__ = "wa_accounts"

    id           = Column(Integer, primary_key=True, index=True)
    label        = Column(String(100), nullable=True)
    phone        = Column(String(30),  nullable=False)
    session_data = Column(Text,        nullable=False)   # JSON auth files
    is_active    = Column(Boolean,     default=True)
    created_at   = Column(DateTime,    default=datetime.utcnow)
