from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import get_current_user, verify_csrf
from app.database import get_db
from app.models import DeviceSession, User
from app.template_utils import jakarta_time


router = APIRouter(prefix="/security", tags=["security"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parents[1] / "templates")
)
templates.env.filters["jakarta_time"] = jakarta_time


@router.get("/sessions")
def sessions_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    now = datetime.utcnow()
    sessions = db.query(DeviceSession).filter(
        DeviceSession.user_id == current_user.id,
        DeviceSession.revoked_at.is_(None),
        DeviceSession.expires_at > now,
    ).order_by(DeviceSession.last_seen_at.desc()).all()
    return templates.TemplateResponse(request, "security_sessions.html", {
        "request": request,
        "current_user": current_user,
        "sessions": sessions,
        "current_session_id": request.state.device_session.id,
        "notice": request.query_params.get("notice"),
    })


@router.post("/sessions/{session_id}/revoke")
def revoke_session(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    device = db.query(DeviceSession).filter(
        DeviceSession.id == session_id,
        DeviceSession.user_id == current_user.id,
        DeviceSession.revoked_at.is_(None),
    ).first()
    if not device:
        raise HTTPException(status_code=404, detail="Sesi tidak ditemukan")
    device.revoked_at = datetime.utcnow()
    db.commit()
    if device.id == request.state.device_session.id:
        request.session.clear()
        return RedirectResponse("/login?notice=session_revoked", status_code=303)
    return RedirectResponse("/security/sessions?notice=session_revoked", status_code=303)


@router.post("/sessions/revoke-others")
def revoke_other_sessions(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    db.query(DeviceSession).filter(
        DeviceSession.user_id == current_user.id,
        DeviceSession.id != request.state.device_session.id,
        DeviceSession.revoked_at.is_(None),
    ).update({DeviceSession.revoked_at: datetime.utcnow()}, synchronize_session=False)
    db.commit()
    return RedirectResponse("/security/sessions?notice=others_revoked", status_code=303)


@router.post("/sessions/revoke-all")
def revoke_all_sessions(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    db.query(DeviceSession).filter(
        DeviceSession.user_id == current_user.id,
        DeviceSession.revoked_at.is_(None),
    ).update({DeviceSession.revoked_at: datetime.utcnow()}, synchronize_session=False)
    db.commit()
    request.session.clear()
    return RedirectResponse("/login?notice=all_sessions_revoked", status_code=303)
