from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.database import Base


class BlastJob(Base):
    __tablename__ = "blast_jobs"

    id = Column(Integer, primary_key=True, index=True)
    status = Column(String(20), nullable=False, default="queued", index=True)
    message = Column(Text, nullable=False)
    image_path = Column(String, nullable=True)
    delay_seconds = Column(Integer, nullable=False, default=5)
    delay_max_seconds = Column(Float, nullable=True, default=None)
    accounts_json = Column(Text, nullable=False, default="[]")
    consent_confirmed = Column(Boolean, nullable=False, default=False)

    total_count = Column(Integer, nullable=False, default=0)
    sent_count = Column(Integer, nullable=False, default=0)
    failed_count = Column(Integer, nullable=False, default=0)
    skipped_count = Column(Integer, nullable=False, default=0)
    pending_count = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    recipients = relationship(
        "BlastRecipient",
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class BlastRecipient(Base):
    __tablename__ = "blast_recipients"
    __table_args__ = (
        UniqueConstraint("job_id", "normalized_username", name="uq_job_username"),
        Index("ix_blast_recipient_target_status", "normalized_username", "status"),
        Index("ix_blast_recipient_sent_at", "sent_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("blast_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("telegram_accounts.id", ondelete="SET NULL"), nullable=True, index=True)

    username = Column(String(255), nullable=False)
    normalized_username = Column(String(255), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)
    status = Column(String(20), nullable=False, default="pending", index=True)
    error = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    sent_at = Column(DateTime, nullable=True)

    job = relationship("BlastJob", back_populates="recipients")
    account = relationship("TelegramAccount")
