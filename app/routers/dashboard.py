from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import BlastJob, BlastRecipient, TelegramAccount

router = APIRouter()
APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


@router.get("/dashboard")
def dashboard(request: Request, db: Session = Depends(get_db)):
    accounts = db.query(TelegramAccount).order_by(TelegramAccount.created_at.desc()).all()
    recent_jobs = db.query(BlastJob).order_by(BlastJob.created_at.desc()).limit(12).all()

    now = datetime.utcnow()
    ranges = [
        ("1 jam", timedelta(hours=1)),
        ("3 jam", timedelta(hours=3)),
        ("6 jam", timedelta(hours=6)),
        ("12 jam", timedelta(hours=12)),
        ("1 hari", timedelta(days=1)),
        ("3 hari", timedelta(days=3)),
        ("7 hari", timedelta(days=7)),
        ("1 bulan", timedelta(days=30)),
    ]
    history_counts = []
    for label, delta in ranges:
        count = (
            db.query(func.count(BlastRecipient.id))
            .filter(
                BlastRecipient.status == "sent",
                BlastRecipient.sent_at >= now - delta,
            )
            .scalar()
            or 0
        )
        history_counts.append({"label": label, "count": count})

    running_jobs = (
        db.query(func.count(BlastJob.id))
        .filter(BlastJob.status.in_(["queued", "running"]))
        .scalar()
        or 0
    )

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "accounts": accounts,
        "recent_jobs": recent_jobs,
        "history_counts": history_counts,
        "running_jobs": running_jobs,
        "error": request.query_params.get("error"),
    })
