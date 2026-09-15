from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship

from app.database import Base


class InboxConversation(Base):
    __tablename__ = "inbox_conversations"
    __table_args__ = (
        UniqueConstraint("account_id", "peer_id", name="uq_inbox_conversation_peer"),
        Index("ix_inbox_conversation_user_state", "user_id", "is_archived", "is_pinned"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("telegram_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    peer_id = Column(BigInteger, nullable=False)
    peer_access_hash = Column(BigInteger, nullable=False)
    peer_name = Column(String(255), nullable=False)
    peer_username = Column(String(255), nullable=True)
    avatar_path = Column(String(500), nullable=True)
    list_label = Column(String(40), nullable=True)
    is_archived = Column(Boolean, nullable=False, default=False)
    is_muted = Column(Boolean, nullable=False, default=False)
    is_pinned = Column(Boolean, nullable=False, default=False)
    is_favorite = Column(Boolean, nullable=False, default=False)
    is_blocked = Column(Boolean, nullable=False, default=False)
    marked_unread = Column(Boolean, nullable=False, default=False)
    auto_replied_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    account = relationship("TelegramAccount")
