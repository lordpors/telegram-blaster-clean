from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.database import Base


class InboxMessage(Base):
    __tablename__ = "inbox_messages"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "peer_id",
            "telegram_message_id",
            name="uq_inbox_account_peer_message",
        ),
        Index("ix_inbox_user_unread", "user_id", "is_read"),
        Index("ix_inbox_conversation", "account_id", "peer_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    account_id = Column(
        Integer,
        ForeignKey("telegram_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    peer_id = Column(BigInteger, nullable=False)
    peer_access_hash = Column(BigInteger, nullable=False)
    peer_name = Column(String(255), nullable=False)
    peer_username = Column(String(255), nullable=True)
    telegram_message_id = Column(Integer, nullable=False)
    direction = Column(String(8), nullable=False)
    body = Column(Text, nullable=False)
    is_read = Column(Boolean, nullable=False, default=False)
    is_archived = Column(Boolean, nullable=False, default=False)
    is_starred = Column(Boolean, nullable=False, default=False)
    media_path = Column(String(500), nullable=True)
    media_name = Column(String(255), nullable=True)
    media_type = Column(String(100), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    account = relationship("TelegramAccount")
