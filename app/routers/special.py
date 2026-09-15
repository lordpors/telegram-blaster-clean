from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import get_current_user, verify_csrf
from app.database import get_db
from app.models import User


router = APIRouter(prefix="/special", tags=["special"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
AUTO_REPLY_MODES = {"cooldown", "always"}


@router.get("")
def special_page(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return templates.TemplateResponse(request, "special.html", {
        "request": request,
        "current_user": current_user,
        "notice": request.query_params.get("notice"),
        "error": request.query_params.get("error"),
    })


@router.post("/auto-reply")
def update_auto_reply(
    enabled: bool = Form(False),
    message: str = Form(""),
    mode: str = Form("cooldown"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    message = message.strip()
    if len(message) > 4096 or (enabled and not message) or mode not in AUTO_REPLY_MODES:
        return RedirectResponse("/special?error=invalid_message", status_code=303)
    current_user.auto_reply_enabled = enabled
    current_user.auto_reply_message = message or None
    current_user.auto_reply_mode = mode
    db.commit()
    return RedirectResponse("/special?notice=saved", status_code=303)
