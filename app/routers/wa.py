"""
WhatsApp router — kelola akun WA + tambah member ke group
"""
import asyncio, glob, json, os, shutil, uuid, tempfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.wa_account import WAAccount

router    = APIRouter()
APP_DIR   = Path(__file__).resolve().parents[1]
WA_SVC    = Path(__file__).resolve().parents[2] / "wa_service"
TMP_MOD   = Path("/tmp/wa_modules")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

_jobs: dict[str, dict] = {}
_bg:   set = set()


def _render(req, tpl, ctx):
    ctx["request"] = req
    return templates.TemplateResponse(tpl, ctx)


# ─── GET /wa  (dashboard akun) ───────────────────────────────────────────────
@router.get("/wa", response_class=HTMLResponse)
def wa_page(request: Request, db: Session = Depends(get_db)):
    accounts = db.query(WAAccount).filter(WAAccount.is_active == True).order_by(WAAccount.created_at.desc()).all()
    return _render(request, "wa.html", {"accounts": accounts})


# ─── GET /wa/connect  (halaman QR) ───────────────────────────────────────────
@router.get("/wa/connect", response_class=HTMLResponse)
def wa_connect_page(request: Request):
    return _render(request, "wa_connect.html", {"job_id": None})


# ─── POST /wa/connect/start  (mulai proses QR) ───────────────────────────────
@router.post("/wa/connect/start", response_class=HTMLResponse)
async def wa_connect_start(request: Request, label: str = Form("")):
    jid      = uuid.uuid4().hex
    out_file = f"/tmp/wa_sess_{jid}.json"
    _jobs[jid] = {
        "status": "connecting", "qr": None,
        "log": ["Memulai..."], "phone": None,
        "out_file": out_file, "label": label.strip(),
    }
    t = asyncio.create_task(_run_connect(jid))
    _bg.add(t); t.add_done_callback(_bg.discard)
    return _render(request, "wa_connect.html", {"job_id": jid})


# ─── POST /wa/connect/save  (simpan akun setelah QR berhasil) ────────────────
@router.post("/wa/connect/save/{jid}")
def wa_connect_save(jid: str, db: Session = Depends(get_db)):
    job = _jobs.get(jid)
    if not job or job["status"] != "connected":
        return JSONResponse({"error": "belum terhubung"}, status_code=400)
    try:
        with open(job["out_file"]) as f:
            data = json.load(f)
    except Exception:
        return JSONResponse({"error": "file sesi tidak ditemukan"}, status_code=400)

    acc = WAAccount(
        label=job.get("label") or data.get("phone", ""),
        phone=data.get("phone", ""),
        session_data=json.dumps(data),
        is_active=True,
        created_at=datetime.utcnow(),
    )
    db.add(acc)
    db.commit()
    try: os.remove(job["out_file"])
    except: pass
    return RedirectResponse(url="/wa", status_code=303)


# ─── GET /api/wa/connect/{jid}  (poll status QR) ─────────────────────────────
@router.get("/api/wa/connect/{jid}")
def wa_connect_poll(jid: str):
    if jid not in _jobs:
        return JSONResponse({"error": "not_found"}, status_code=404)
    j = _jobs[jid]
    return JSONResponse({"status": j["status"], "qr": j["qr"],
                         "log": j["log"], "phone": j.get("phone")})


# ─── POST /wa/delete/{account_id}  (hapus akun) ──────────────────────────────
@router.post("/wa/delete/{account_id}")
def wa_delete(account_id: int, db: Session = Depends(get_db)):
    acc = db.get(WAAccount, account_id)
    if acc:
        db.delete(acc)
        db.commit()
    return RedirectResponse(url="/wa", status_code=303)


# ─── POST /api/wa/groups/start  (muat group dari akun) ───────────────────────
@router.post("/api/wa/groups/start")
async def wa_groups_start(request: Request, db: Session = Depends(get_db)):
    body       = await request.json()
    account_id = body.get("account_id")
    acc        = db.get(WAAccount, account_id)
    if not acc:
        return JSONResponse({"error": "Akun tidak ditemukan"}, status_code=404)

    jid      = uuid.uuid4().hex
    sess_tmp = f"/tmp/wa_s_{jid}.json"
    job_tmp  = f"/tmp/wa_j_{jid}.json"

    with open(sess_tmp, "w") as f: f.write(acc.session_data)
    with open(job_tmp,  "w") as f: json.dump({"mode": "list_groups"}, f)

    _jobs[jid] = {"status": "loading", "groups": [], "log": ["Menghubungkan..."],
                  "sess_tmp": sess_tmp, "job_tmp": job_tmp}
    t = asyncio.create_task(_run_add(jid))
    _bg.add(t); t.add_done_callback(_bg.discard)
    return JSONResponse({"job_id": jid})


# ─── POST /api/wa/add/start  (mulai tambah member) ───────────────────────────
@router.post("/api/wa/add/start")
async def wa_add_start(request: Request, db: Session = Depends(get_db)):
    body       = await request.json()
    account_id = body.get("account_id")
    group_id   = body.get("group_id")
    numbers    = body.get("numbers", [])
    acc        = db.get(WAAccount, account_id)
    if not acc or not group_id or not numbers:
        return JSONResponse({"error": "Data tidak lengkap"}, status_code=400)

    jid      = uuid.uuid4().hex
    sess_tmp = f"/tmp/wa_s_{jid}.json"
    job_tmp  = f"/tmp/wa_j_{jid}.json"

    with open(sess_tmp, "w") as f: f.write(acc.session_data)
    with open(job_tmp,  "w") as f:
        json.dump({"mode": "add_members", "group_id": group_id, "numbers": numbers}, f)

    _jobs[jid] = {
        "status": "running", "log": ["Memulai..."],
        "added": 0, "failed": 0, "total": len(numbers),
        "results": [], "sess_tmp": sess_tmp, "job_tmp": job_tmp,
    }
    t = asyncio.create_task(_run_add(jid))
    _bg.add(t); t.add_done_callback(_bg.discard)
    return JSONResponse({"job_id": jid})


# ─── GET /api/wa/poll/{jid}  (poll status apapun) ────────────────────────────
@router.get("/api/wa/poll/{jid}")
def wa_poll(jid: str):
    if jid not in _jobs:
        return JSONResponse({"error": "not_found"}, status_code=404)
    j = _jobs[jid]
    return JSONResponse({
        "status":  j["status"],
        "log":     j.get("log", []),
        "groups":  j.get("groups", []),
        "added":   j.get("added", 0),
        "failed":  j.get("failed", 0),
        "total":   j.get("total", 0),
        "results": j.get("results", []),
    })


# ─── Background: jalankan wa_connect.js ──────────────────────────────────────
async def _run_connect(jid: str):
    job  = _jobs[jid]
    node = _find_node()
    if not node:
        job["status"] = "error"; job["log"].append("Node.js tidak ditemukan"); return

    await _ensure_modules(job)
    if job["status"] == "error": return

    script = WA_SVC / "wa_connect.js"
    proc   = await asyncio.create_subprocess_exec(
        node, str(script), job["out_file"],
        cwd=str(TMP_MOD),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "NODE_PATH": str(TMP_MOD / "node_modules")},
    )

    async def drain():
        async for raw in proc.stderr:
            l = raw.decode(errors="ignore").strip()
            if l and "Warning" not in l: job["log"].append(f"[node] {l[:200]}")
    asyncio.create_task(drain())

    async for raw in proc.stdout:
        line = raw.decode(errors="ignore").strip()
        if not line: continue
        try: msg = json.loads(line)
        except: job["log"].append(line[:150]); continue
        t = msg.get("type")
        if t == "qr":        job["qr"] = msg["data"]; job["status"] = "waiting_scan"
        elif t == "log":     job["log"].append(msg.get("message", ""))
        elif t == "connected":
            job["status"] = "connected"; job["phone"] = msg.get("phone")
            job["log"].append(f"Terhubung sebagai {msg.get('phone')}")
        elif t == "error":   job["status"] = "error"; job["log"].append(f"Error: {msg['message']}")
    await proc.wait()


# ─── Background: jalankan wa_add.js ──────────────────────────────────────────
async def _run_add(jid: str):
    job  = _jobs[jid]
    node = _find_node()
    if not node:
        job["status"] = "error"; job["log"].append("Node.js tidak ditemukan"); return

    await _ensure_modules(job)
    if job["status"] == "error": return

    script = WA_SVC / "wa_add.js"
    proc   = await asyncio.create_subprocess_exec(
        node, str(script), job["sess_tmp"], job["job_tmp"],
        cwd=str(TMP_MOD),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "NODE_PATH": str(TMP_MOD / "node_modules")},
    )

    async def drain():
        async for raw in proc.stderr:
            l = raw.decode(errors="ignore").strip()
            if l and "Warning" not in l: job["log"].append(f"[node] {l[:200]}")
    asyncio.create_task(drain())

    async for raw in proc.stdout:
        line = raw.decode(errors="ignore").strip()
        if not line: continue
        try: msg = json.loads(line)
        except: job["log"].append(line[:150]); continue
        t = msg.get("type")
        if   t == "log":      job["log"].append(msg.get("message", ""))
        elif t == "groups":   job["groups"] = msg["data"]; job["status"] = "done"
        elif t == "result":   job["results"].append({"number": msg["number"], "status": msg["status"]})
        elif t == "progress": job.update({"added": msg["added"], "failed": msg["failed"], "total": msg["total"]})
        elif t == "done":
            job.update({"status": "done", "added": msg["added"],
                        "failed": msg["failed"], "total": msg["total"]})
            job["log"].append(f"Selesai — {msg['added']} berhasil, {msg['failed']} gagal")
        elif t == "error":    job["status"] = "error"; job["log"].append(f"Error: {msg['message']}")

    await proc.wait()
    # Cleanup temp files
    for k in ("sess_tmp", "job_tmp"):
        try: os.remove(job.get(k, ""))
        except: pass


# ─── Helpers ─────────────────────────────────────────────────────────────────
async def _ensure_modules(job: dict):
    TMP_MOD.mkdir(parents=True, exist_ok=True)
    pkg_dst = TMP_MOD / "package.json"
    if not pkg_dst.exists():
        shutil.copy(WA_SVC / "package.json", pkg_dst)

    if not (TMP_MOD / "node_modules" / "@whiskeysockets").exists():
        job["log"].append("Menginstall Baileys (~30 detik, hanya sekali)...")
        p = await asyncio.create_subprocess_exec(
            "npm", "install", "--omit=dev",
            cwd=str(TMP_MOD),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await p.communicate()
        if p.returncode != 0:
            job["status"] = "error"
            job["log"].append(f"npm install gagal: {err.decode()[:200]}")
            return
        job["log"].append("Baileys siap.")


def _find_node():
    n = shutil.which("node")
    if n: return n
    for pat in ["/mise/shims/node", "/mise/installs/node/*/bin/node",
                "/root/.local/share/mise/installs/node/*/bin/node",
                "/nix/store/*/bin/node", "/usr/bin/node"]:
        if "*" in pat:
            m = sorted(glob.glob(pat), reverse=True)
            if m: return m[0]
        elif os.path.exists(pat): return pat
    return None


# ─── GET /wa/add  (halaman tambah member) ────────────────────────────────────
@router.get("/wa/add", response_class=HTMLResponse)
def wa_add_page(request: Request, account_id: int = None, db: Session = Depends(get_db)):
    accounts = db.query(WAAccount).filter(WAAccount.is_active == True).all()
    return _render(request, "wa_add.html", {
        "accounts":    accounts,
        "selected_id": account_id,
    })
