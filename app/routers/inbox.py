import logging
import mimetypes
import uuid
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user, verify_csrf
from app.database import get_db
from app.models import InboxConversation, InboxMessage, TelegramAccount, User
from app.services.inbox_manager import INBOX_MEDIA_DIR, MAX_INBOX_MEDIA_BYTES, inbox_manager
from app.template_utils import jakarta_time


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
templates.env.filters["jakarta_time"] = jakarta_time
logger = logging.getLogger(__name__)
VALID_VIEWS = {"all", "unread", "favorite", "archived", "starred"}


def _view(value: str) -> str:
    return value if value in VALID_VIEWS else "all"


def _inbox_url(view: str = "all", **params) -> str:
    query = {key: value for key, value in params.items() if value is not None}
    if view != "all":
        query["view"] = view
    return "/inbox" + (f"?{urlencode(query)}" if query else "")


def _conversation(db: Session, user_id: int, account_id: int, peer_id: int):
    return db.query(InboxConversation).filter(
        InboxConversation.user_id == user_id,
        InboxConversation.account_id == account_id,
        InboxConversation.peer_id == peer_id,
    ).first()


def _safe_media_path(raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    path = Path(raw_path).expanduser().resolve()
    return path if path.is_relative_to(INBOX_MEDIA_DIR) else None


def _delete_media(messages: list[InboxMessage]) -> None:
    for message in messages:
        path = _safe_media_path(message.media_path)
        if path:
            path.unlink(missing_ok=True)


@router.get("/inbox")
def inbox_page(
    request: Request,
    account_id: int | None = None,
    peer_id: int | None = None,
    view: str = "all",
    archived: bool = False,
    list_name: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    view = "archived" if archived else _view(view)
    list_name = (list_name or "").strip()[:40] or None
    states = (
        db.query(InboxConversation)
        .options(joinedload(InboxConversation.account))
        .filter(InboxConversation.user_id == current_user.id)
        .order_by(InboxConversation.is_pinned.desc(), InboxConversation.updated_at.desc())
        .limit(500)
        .all()
    )
    # ponytail: one recent window avoids per-chat queries; use SQL window functions past 500 chats.
    recent = (
        db.query(InboxMessage)
        .filter(InboxMessage.user_id == current_user.id)
        .order_by(InboxMessage.created_at.desc(), InboxMessage.id.desc())
        .limit(3000)
        .all()
    )
    latest_by_key = {}
    unread_by_key = {}
    starred_keys = set()
    for message in recent:
        key = (message.account_id, message.peer_id)
        latest_by_key.setdefault(key, message)
        if message.direction == "in" and not message.is_read:
            unread_by_key[key] = unread_by_key.get(key, 0) + 1
        if message.is_starred:
            starred_keys.add(key)

    conversations = []
    counts = {"all": 0, "unread": 0, "favorite": 0, "archived": 0, "starred": 0}
    for state in states:
        key = (state.account_id, state.peer_id)
        unread_count = max(unread_by_key.get(key, 0), int(state.marked_unread))
        counts["archived" if state.is_archived else "all"] += 1
        counts["unread"] += bool(unread_count and not state.is_archived)
        counts["favorite"] += bool(state.is_favorite and not state.is_archived)
        counts["starred"] += key in starred_keys
        visible = bool(state.list_label == list_name and not state.is_archived) if list_name else {
            "all": not state.is_archived,
            "unread": bool(unread_count and not state.is_archived),
            "favorite": bool(state.is_favorite and not state.is_archived),
            "archived": state.is_archived,
            "starred": key in starred_keys,
        }[view]
        if visible:
            conversations.append({
                "state": state,
                "account_id": state.account_id,
                "account_label": state.account.label or state.account.phone,
                "peer_id": state.peer_id,
                "peer_name": state.peer_name,
                "peer_username": state.peer_username,
                "latest": latest_by_key.get(key),
                "unread": unread_count,
                "url": _inbox_url(view, account_id=state.account_id, peer_id=state.peer_id, list_name=list_name),
            })

    selected = None
    messages = []
    if account_id is not None and peer_id is not None:
        selected_item = next((item for item in conversations if (
            item["account_id"], item["peer_id"]
        ) == (account_id, peer_id)), None)
        if not selected_item:
            raise HTTPException(status_code=404, detail="Percakapan tidak ditemukan")
        selected = selected_item["state"]
        selected.account_label = selected_item["account_label"]
        message_query = db.query(InboxMessage).filter(
                InboxMessage.user_id == current_user.id,
                InboxMessage.account_id == account_id,
                InboxMessage.peer_id == peer_id,
            )
        if view == "starred":
            message_query = message_query.filter(InboxMessage.is_starred.is_(True))
        messages = list(reversed(
            message_query.order_by(InboxMessage.created_at.desc(), InboxMessage.id.desc()).limit(200).all()
        ))
        db.query(InboxMessage).filter(
            InboxMessage.user_id == current_user.id,
            InboxMessage.account_id == account_id,
            InboxMessage.peer_id == peer_id,
            InboxMessage.direction == "in",
            InboxMessage.is_read.is_(False),
        ).update({InboxMessage.is_read: True}, synchronize_session=False)
        selected.marked_unread = False
        db.commit()
        selected_item["unread"] = 0

    return templates.TemplateResponse(request, "inbox.html", {
        "request": request,
        "conversations": conversations,
        "selected": selected,
        "messages": messages,
        "view": view,
        "counts": counts,
        "active_list": list_name,
        "list_filters": [{
            "label": label,
            "url": _inbox_url(list_name=label),
        } for label in sorted({state.list_label for state in states if state.list_label and not state.is_archived})],
        "back_url": _inbox_url(view, list_name=list_name),
        "error": request.query_params.get("error"),
        "notice": request.query_params.get("notice"),
    })


@router.post("/inbox/conversation/{action}")
async def manage_conversation(
    action: str,
    account_id: int = Form(...),
    peer_id: int = Form(...),
    view: str = Form("all"),
    archived: bool = Form(False),
    list_label: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    view = "archived" if archived else _view(view)
    conversation = _conversation(db, current_user.id, account_id, peer_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Percakapan tidak ditemukan")

    messages = db.query(InboxMessage).filter(
        InboxMessage.user_id == current_user.id,
        InboxMessage.account_id == account_id,
        InboxMessage.peer_id == peer_id,
    )
    if action in {"block", "unblock"}:
        try:
            successful_ids, connected_count = await inbox_manager.set_blocked_all(
                current_user.id,
                peer_id,
                conversation.peer_username,
                action == "block",
            )
        except Exception:
            logger.exception("Telegram block sync failed for user %s", current_user.id)
            return RedirectResponse(_inbox_url(view, account_id=account_id, peer_id=peer_id, error="action_failed"), status_code=303)
        if not successful_ids:
            return RedirectResponse(_inbox_url(view, account_id=account_id, peer_id=peer_id, error="action_failed"), status_code=303)
        db.query(InboxConversation).filter(
            InboxConversation.user_id == current_user.id,
            InboxConversation.peer_id == peer_id,
            InboxConversation.account_id.in_(successful_ids),
        ).update({InboxConversation.is_blocked: action == "block"}, synchronize_session=False)
        db.commit()
        notice = "blocked_all" if len(successful_ids) == connected_count else "blocked_partial"
        if action == "unblock":
            notice = "unblocked_all" if len(successful_ids) == connected_count else "unblocked_partial"
        return RedirectResponse(_inbox_url(view, account_id=account_id, peer_id=peer_id, notice=notice), status_code=303)
    elif action == "archive":
        conversation.is_archived = True
        messages.update({InboxMessage.is_read: True}, synchronize_session=False)
    elif action == "restore":
        conversation.is_archived = False
    elif action == "mute":
        conversation.is_muted = True
    elif action == "unmute":
        conversation.is_muted = False
    elif action == "pin":
        conversation.is_pinned = True
    elif action == "unpin":
        conversation.is_pinned = False
    elif action == "favorite":
        conversation.is_favorite = True
    elif action == "unfavorite":
        conversation.is_favorite = False
    elif action == "unread":
        conversation.marked_unread = True
    elif action == "read":
        conversation.marked_unread = False
        messages.update({InboxMessage.is_read: True}, synchronize_session=False)
    elif action == "list":
        conversation.list_label = list_label.strip()[:40] or None
    elif action == "clear":
        stored = messages.all()
        _delete_media(stored)
        messages.delete(synchronize_session=False)
    elif action == "delete":
        stored = messages.all()
        _delete_media(stored)
        messages.delete(synchronize_session=False)
        db.delete(conversation)
    else:
        raise HTTPException(status_code=404, detail="Aksi tidak ditemukan")
    db.commit()

    if action in {"delete", "clear", "archive", "restore", "unread"}:
        return RedirectResponse(_inbox_url(view, notice=f"{action}d"), status_code=303)
    return RedirectResponse(_inbox_url(view, account_id=account_id, peer_id=peer_id, notice="updated"), status_code=303)


def _parse_keys(values: list[str]) -> list[tuple[int, int]]:
    keys = []
    for value in values[:100]:
        try:
            account_id, peer_id = value.split(":", 1)
            keys.append((int(account_id), int(peer_id)))
        except (TypeError, ValueError):
            continue
    return keys


@router.post("/inbox/conversations/bulk")
def bulk_conversations(
    action: str = Form(...),
    conversation_keys: list[str] = Form([]),
    view: str = Form("all"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    if action not in {"read", "mute", "archive", "delete"}:
        raise HTTPException(status_code=404, detail="Aksi tidak ditemukan")
    affected = 0
    for account_id, peer_id in _parse_keys(conversation_keys):
        conversation = _conversation(db, current_user.id, account_id, peer_id)
        if not conversation:
            continue
        messages = db.query(InboxMessage).filter(
            InboxMessage.user_id == current_user.id,
            InboxMessage.account_id == account_id,
            InboxMessage.peer_id == peer_id,
        )
        if action == "read":
            conversation.marked_unread = False
            messages.update({InboxMessage.is_read: True}, synchronize_session=False)
        elif action == "mute":
            conversation.is_muted = True
        elif action == "archive":
            conversation.is_archived = True
            messages.update({InboxMessage.is_read: True}, synchronize_session=False)
        else:
            stored = messages.all()
            _delete_media(stored)
            messages.delete(synchronize_session=False)
            db.delete(conversation)
        affected += 1
    db.commit()
    return RedirectResponse(_inbox_url(_view(view), notice="bulk_updated", count=affected), status_code=303)


@router.post("/inbox/read-all")
def read_all(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    db.query(InboxMessage).filter(
        InboxMessage.user_id == current_user.id,
        InboxMessage.direction == "in",
    ).update({InboxMessage.is_read: True}, synchronize_session=False)
    db.query(InboxConversation).filter(
        InboxConversation.user_id == current_user.id,
    ).update({InboxConversation.marked_unread: False}, synchronize_session=False)
    db.commit()
    return RedirectResponse(_inbox_url(notice="all_read"), status_code=303)


@router.post("/inbox/message/{message_id}/star")
def star_message(
    message_id: int,
    account_id: int = Form(...),
    peer_id: int = Form(...),
    view: str = Form("all"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    message = db.query(InboxMessage).filter(
        InboxMessage.id == message_id,
        InboxMessage.user_id == current_user.id,
        InboxMessage.account_id == account_id,
        InboxMessage.peer_id == peer_id,
    ).first()
    if not message:
        raise HTTPException(status_code=404, detail="Pesan tidak ditemukan")
    message.is_starred = not message.is_starred
    db.commit()
    return RedirectResponse(_inbox_url(_view(view), account_id=account_id, peer_id=peer_id), status_code=303)


@router.post("/inbox/reply")
async def reply(
    account_id: int = Form(...),
    peer_id: int = Form(...),
    view: str = Form("all"),
    body: str = Form(""),
    voice_note: bool = Form(False),
    attachment: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
):
    body = body.strip()
    view = _view(view)
    destination = _inbox_url(
        view,
        account_id=None if view == "unread" else account_id,
        peer_id=None if view == "unread" else peer_id,
    )
    has_file = bool(attachment and attachment.filename)
    if (not body and not has_file) or len(body) > 4096:
        return RedirectResponse(f"{destination}&error=invalid_message", status_code=303)

    account = db.query(TelegramAccount).filter(
        TelegramAccount.id == account_id,
        TelegramAccount.user_id == current_user.id,
    ).first()
    conversation = _conversation(db, current_user.id, account_id, peer_id)
    if not account or not conversation:
        raise HTTPException(status_code=404, detail="Percakapan tidak ditemukan")
    if conversation.is_blocked:
        return RedirectResponse(f"{destination}&error=blocked", status_code=303)

    media_path = None
    media_name = None
    media_type = None
    if has_file:
        media_name = Path(attachment.filename).name[:255]
        suffix = Path(media_name).suffix[:12]
        media_type = attachment.content_type or mimetypes.guess_type(media_name)[0] or "application/octet-stream"
        directory = INBOX_MEDIA_DIR / str(current_user.id) / str(account_id) / str(peer_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{uuid.uuid4().hex}{suffix}"
        size = 0
        with path.open("wb") as output:
            while chunk := await attachment.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_INBOX_MEDIA_BYTES:
                    output.close()
                    path.unlink(missing_ok=True)
                    return RedirectResponse(f"{destination}&error=file_too_large", status_code=303)
                output.write(chunk)
        media_path = str(path.resolve())

    try:
        send_options = {"file_path": media_path, "voice_note": voice_note} if media_path else {}
        telegram_message_id, created_at = await inbox_manager.send_reply(
            account.id, peer_id, conversation.peer_access_hash, body, **send_options
        )
    except Exception:
        logger.exception("Telegram inbox reply failed for account %s", account.id)
        if media_path:
            Path(media_path).unlink(missing_ok=True)
        return RedirectResponse(f"{destination}&error=send_failed", status_code=303)

    db.add(InboxMessage(
        user_id=current_user.id,
        account_id=account.id,
        peer_id=peer_id,
        peer_access_hash=conversation.peer_access_hash,
        peer_name=conversation.peer_name,
        peer_username=conversation.peer_username,
        telegram_message_id=telegram_message_id,
        direction="out",
        body=body or f"📎 {media_name}",
        is_read=True,
        media_path=media_path,
        media_name=media_name,
        media_type=media_type,
        created_at=created_at,
    ))
    conversation.is_archived = False
    conversation.updated_at = created_at
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    return RedirectResponse(destination, status_code=303)


@router.get("/inbox/media/{message_id}")
def inbox_media(
    message_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    message = db.query(InboxMessage).filter(
        InboxMessage.id == message_id,
        InboxMessage.user_id == current_user.id,
    ).first()
    path = _safe_media_path(message.media_path if message else None)
    if not path or not path.is_file():
        raise HTTPException(status_code=404, detail="Lampiran tidak ditemukan")
    media_type = message.media_type or "application/octet-stream"
    if media_type in {"image/jpeg", "image/png", "image/gif", "image/webp", "audio/mpeg", "audio/ogg", "audio/webm", "audio/wav"}:
        return FileResponse(path, media_type=media_type)
    return FileResponse(path, media_type="application/octet-stream", filename=message.media_name or path.name)


@router.get("/inbox/avatar/{conversation_id}")
async def inbox_avatar(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conversation = db.query(InboxConversation).filter(
        InboxConversation.id == conversation_id,
        InboxConversation.user_id == current_user.id,
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Percakapan tidak ditemukan")
    path = _safe_media_path(conversation.avatar_path)
    if not path or not path.is_file():
        target = INBOX_MEDIA_DIR / str(current_user.id) / str(conversation.account_id) / str(conversation.peer_id) / "avatar.jpg"
        try:
            path = await inbox_manager.download_avatar(
                conversation.account_id,
                conversation.peer_id,
                conversation.peer_access_hash,
                target,
            )
        except Exception:
            path = None
        if path:
            conversation.avatar_path = str(path)
            db.commit()
        else:
            conversation.avatar_path = "-"
            db.commit()
    if not path or not path.is_file():
        raise HTTPException(status_code=404, detail="Foto profil tidak tersedia")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/api/inbox/unread")
def unread(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account_ids = {
        row[0] for row in db.query(TelegramAccount.id).filter(
            TelegramAccount.user_id == current_user.id,
        )
    }
    join_condition = and_(
        InboxConversation.account_id == InboxMessage.account_id,
        InboxConversation.peer_id == InboxMessage.peer_id,
        InboxConversation.user_id == InboxMessage.user_id,
    )
    query = db.query(InboxMessage).join(InboxConversation, join_condition).filter(
        InboxMessage.user_id == current_user.id,
        InboxMessage.direction == "in",
        InboxMessage.is_read.is_(False),
        InboxConversation.is_archived.is_(False),
    )
    count = query.count()
    latest = query.filter(InboxConversation.is_muted.is_(False)).options(
        joinedload(InboxMessage.account)
    ).order_by(InboxMessage.created_at.desc(), InboxMessage.id.desc()).first()
    return JSONResponse({
        "unread_count": count,
        "latest_id": latest.id if latest else None,
        "latest_preview": latest.body[:120] if latest else None,
        "peer_name": latest.peer_name if latest else None,
        "account_label": (latest.account.label or latest.account.phone) if latest else None,
        "url": _inbox_url(account_id=latest.account_id, peer_id=latest.peer_id) if latest else "/inbox",
        "connected_account_ids": [
            account_id for account_id in account_ids if inbox_manager.connected(account_id)
        ],
    })
