import hashlib
import html
import json
import os
import secrets
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

from ai_parser import parse_document_file, parse_image_file, parse_text
from crawler import crawl_all as _do_crawl_all
from auth import create_token, verify_token
from database import SessionLocal, get_db, init_db
from member_auth import create_member_token, hash_password, verify_member_token, verify_password
from models import (
    BOARDS,
    ChatMessage, ChatRoom, ChatRoomMember,
    Comment, CommentLike,
    Competition, InviteCode, InviteCodeUseLog, Member,
    Post, PostLike,
    Team, TeamMember,
)

app = FastAPI(title="공모전 보드")

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "uploads"))
if not UPLOAD_DIR.is_absolute():
    UPLOAD_DIR = BASE_DIR / UPLOAD_DIR
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    accepts_html = "text/html" in request.headers.get("accept", "")
    if not accepts_html:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    detail = html.escape(exc.detail if isinstance(exc.detail, str) else "요청을 처리할 수 없습니다.")
    return HTMLResponse(
        f"""
        <!doctype html>
        <html lang="ko">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>오류 · 공모전 보드</title>
          <link rel="stylesheet" href="/static/css/style.css">
        </head>
        <body>
          <main class="auth-page">
            <section class="auth-panel">
              <p class="eyebrow">Error {exc.status_code}</p>
              <h1>요청을 처리하지 못했습니다.</h1>
              <p class="muted">{detail}</p>
              <div class="modal-actions">
                <a href="javascript:history.back()" class="btn btn-outline">이전으로</a>
                <a href="/" class="btn btn-primary">홈으로</a>
              </div>
            </section>
          </main>
        </body>
        </html>
        """,
        status_code=exc.status_code,
        headers=exc.headers,
    )


def _from_json(value) -> list:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def _optional_int(value, field_name: str) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} 값이 올바른 숫자가 아닙니다.") from exc


def _compact_text(value: str) -> str:
    return "".join((value or "").lower().split())


def _compact_column(column):
    return func.replace(func.replace(func.lower(column), " ", ""), "\t", "")


def _parse_expiry(valid_days: Optional[str], expires_at: Optional[str]) -> Optional[datetime]:
    if expires_at:
        try:
            return datetime.fromisoformat(expires_at)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="만료일 형식이 올바르지 않습니다.") from exc
    days = _optional_int(valid_days, "유효 기간")
    if days:
        return datetime.now() + timedelta(days=days)
    return None


templates.env.filters["fromjson"] = _from_json

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin1234")
TAGS = ["IT/SW", "디자인", "기획·마케팅", "사회혁신", "예술·문화", "창업·스타트업", "논문·학술", "기타"]
ROLES = ["기획", "개발", "디자인", "마케팅", "기타"]
AWARD_RANKS = ["대상", "최우수상", "우수상", "장려상", "입선"]


@app.on_event("startup")
def startup():
    init_db()


# ── 날짜 / 상태 헬퍼 ──────────────────────────────────────────────────────────

def _days_left(deadline: date) -> int:
    return (deadline - date.today()).days


def _urgency(deadline: date) -> str:
    d = _days_left(deadline)
    if d < 0:    return "closed"
    if d <= 7:   return "urgent"
    if d <= 30:  return "soon"
    return "open"


def _annotate(competitions: list) -> list:
    for c in competitions:
        c.status = _urgency(c.deadline)
        c.days_left = _days_left(c.deadline)
    return competitions


# ── 인증 헬퍼 ─────────────────────────────────────────────────────────────────

def _is_admin(request: Request) -> bool:
    token = request.cookies.get("admin_token")
    return bool(token and verify_token(token))


def _admin_redirect(request: Request):
    if not _is_admin(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    return None


def _current_member(request: Request, db: Session) -> Optional[Member]:
    token = request.cookies.get("member_token")
    if not token:
        return None
    mid = verify_member_token(token)
    if not mid:
        return None
    return db.query(Member).filter(Member.id == mid).first()


def _is_privileged(request: Request, db: Session) -> bool:
    if _is_admin(request):
        return True
    m = _current_member(request, db)
    return bool(m and m.role == "sub_admin")


def _ctx(request: Request, db: Session, **extra) -> dict:
    is_admin = _is_admin(request)
    cm = _current_member(request, db)
    base = {
        "request": request,
        "is_admin": is_admin,
        "current_member": cm,
        "is_privileged": is_admin or bool(cm and cm.role == "sub_admin"),
        "boards": BOARDS,
        "now": datetime.now(),
    }
    base.update(extra)
    return base


def _render(request: Request, name: str, context: dict, status_code: int = 200):
    return templates.TemplateResponse(name=name, request=request, context=context, status_code=status_code)


# ── 파일 저장 헬퍼 ────────────────────────────────────────────────────────────

async def _save_image(upload: Optional[UploadFile]) -> Optional[str]:
    if not upload or not upload.filename:
        return None
    content = await upload.read()
    if not content:
        return None
    ext = Path(upload.filename).suffix.lower()
    name = f"{uuid.uuid4().hex}{ext}"
    (UPLOAD_DIR / name).write_bytes(content)
    return name


async def _save_images(uploads: List[UploadFile]) -> list:
    saved = []
    for up in uploads or []:
        name = await _save_image(up)
        if name:
            saved.append(name)
    return saved


async def _save_files(files: List[UploadFile]) -> list:
    saved = []
    for f in files or []:
        if not f.filename:
            continue
        content = await f.read()
        if not content:
            continue
        ext = Path(f.filename).suffix.lower()
        name = f"{uuid.uuid4().hex}{ext}"
        (UPLOAD_DIR / name).write_bytes(content)
        saved.append({"name": f.filename, "path": name})
    return saved


# ── 팀원 비밀번호 헬퍼 (기존 SHA-256 방식 유지) ──────────────────────────────

def _hash_pw(password: str) -> str:
    salt = os.urandom(16).hex()
    h = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return f"{salt}:{h}"


def _verify_pw(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":", 1)
        return hashlib.sha256(f"{salt}{password}".encode()).hexdigest() == h
    except Exception:
        return False


# ── 공통 헬퍼: 회원 이름 매핑 ────────────────────────────────────────────────

def _member_map(db: Session, ids: list[int]) -> dict[int, Member]:
    if not ids:
        return {}
    members = db.query(Member).filter(Member.id.in_(ids)).all()
    return {m.id: m for m in members}


def _chat_member(db: Session, room_id: int, member_id: int) -> Optional[ChatRoomMember]:
    return (
        db.query(ChatRoomMember)
        .filter(ChatRoomMember.room_id == room_id, ChatRoomMember.member_id == member_id)
        .first()
    )


def _ensure_chat_member(db: Session, room: ChatRoom, member: Member) -> ChatRoomMember:
    row = _chat_member(db, room.id, member.id)
    if row:
        if member.id == room.created_by_id and row.role != "owner":
            row.role = "owner"
            db.commit()
        return row
    role = "owner" if member.id == room.created_by_id else "member"
    row = ChatRoomMember(room_id=room.id, member_id=member.id, role=role)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _room_members(db: Session, room_id: int) -> list[ChatRoomMember]:
    rows = (
        db.query(ChatRoomMember)
        .filter(ChatRoomMember.room_id == room_id)
        .order_by(
            case(
                (ChatRoomMember.role == "owner", 0),
                (ChatRoomMember.role == "co_owner", 1),
                else_=2,
            ),
            ChatRoomMember.joined_at.asc(),
        )
        .all()
    )
    members = _member_map(db, [row.member_id for row in rows])
    for row in rows:
        row.member = members.get(row.member_id)
    return rows


def _can_manage_room(room_member: Optional[ChatRoomMember], request: Request, db: Session) -> bool:
    return _is_privileged(request, db) or bool(room_member and room_member.role in ("owner", "co_owner"))


def _is_comment_muted(member: Member) -> bool:
    return bool(member.comment_muted_until and member.comment_muted_until > datetime.now())


# ════════════════════════════════════════════════════════════════════════════
#  공개 페이지 — 공모전
# ════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    tag: str = "",
    sort: str = "deadline",
    q: str = "",
    db: Session = Depends(get_db),
):
    today = date.today()
    active_priority = case((Competition.deadline < today, 1), else_=0)
    featured_priority = case(
        (Competition.is_featured.is_(True), 0),
        (Competition.deadline <= today + timedelta(days=14), 1),
        else_=2,
    )

    featured = (
        db.query(Competition)
        .filter(Competition.deadline >= today)
        .order_by(featured_priority.asc(), Competition.view_count.desc(), Competition.deadline.asc())
        .limit(4)
        .all()
    )
    _annotate(featured)

    query = db.query(Competition)
    if tag and tag != "all":
        query = query.filter(Competition.tags.like(f'%"{tag}"%'))
    if q:
        compact_q = _compact_text(q)
        query = query.filter(
            or_(
                _compact_column(Competition.title).contains(compact_q),
                _compact_column(Competition.organizer).contains(compact_q),
            )
        )

    if sort == "views":
        query = query.order_by(Competition.view_count.desc(), active_priority.asc(), Competition.deadline.asc())
    elif sort == "newest":
        query = query.order_by(Competition.created_at.desc())
    else:
        query = query.order_by(active_priority.asc(), Competition.deadline.asc())

    competitions = _annotate(query.all())

    all_ids = [c.id for c in competitions] + [c.id for c in featured]
    counts = dict(
        db.query(TeamMember.competition_id, func.count(TeamMember.id))
        .filter(TeamMember.competition_id.in_(all_ids))
        .group_by(TeamMember.competition_id)
        .all()
    ) if all_ids else {}
    for c in competitions + featured:
        c.member_count = counts.get(c.id, 0)

    return _render(request,
        "index.html",
        _ctx(request, db,
             featured=featured, competitions=competitions,
             tags=TAGS, current_tag=tag or "all",
             current_sort=sort, query=q, today=today),
    )


@app.get("/competition/{comp_id}", response_class=HTMLResponse)
async def detail(request: Request, comp_id: int, db: Session = Depends(get_db)):
    comp = db.query(Competition).filter(Competition.id == comp_id).first()
    if not comp:
        raise HTTPException(status_code=404, detail="공모전을 찾을 수 없습니다.")

    comp.view_count += 1
    db.commit()
    db.refresh(comp)

    comp.status = _urgency(comp.deadline)
    comp.days_left = _days_left(comp.deadline)

    teams = (
        db.query(Team)
        .filter(Team.competition_id == comp_id)
        .order_by(Team.created_at.asc())
        .all()
    )
    team_ids = [t.id for t in teams]
    all_tm = (
        db.query(TeamMember)
        .filter(TeamMember.team_id.in_(team_ids))
        .order_by(TeamMember.created_at.asc())
        .all()
    ) if team_ids else []
    # team별 멤버 맵
    tm_by_team: dict = {}
    for tm in all_tm:
        tm_by_team.setdefault(tm.team_id, []).append(tm)
    for t in teams:
        t.members = tm_by_team.get(t.id, [])

    today = date.today()
    submission_window = comp.deadline < today <= comp.deadline + timedelta(days=7)

    return _render(request,
        "detail.html",
        _ctx(request, db,
             comp=comp, files=_from_json(comp.files),
             tags_list=_from_json(comp.tags),
             teams=teams, roles=ROLES,
             submission_window=submission_window, today=today),
    )


# ════════════════════════════════════════════════════════════════════════════
#  관리자 — 인증 / 대시보드
# ════════════════════════════════════════════════════════════════════════════

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request, db: Session = Depends(get_db)):
    if _is_admin(request):
        return RedirectResponse(url="/admin", status_code=303)
    return _render(request, "admin/login.html", _ctx(request, db, error=None))


@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...), db: Session = Depends(get_db)):
    if password == ADMIN_PASSWORD:
        resp = RedirectResponse(url="/admin", status_code=303)
        resp.set_cookie("admin_token", create_token(), httponly=True, max_age=86400, samesite="lax")
        return resp
    return _render(request,
        "admin/login.html",
        _ctx(request, db, error="비밀번호가 올바르지 않습니다."),
        status_code=401,
    )


@app.get("/admin/logout")
async def admin_logout():
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie("admin_token")
    return resp


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    if r := _admin_redirect(request):
        return r
    competitions = _annotate(db.query(Competition).order_by(Competition.deadline.asc()).all())
    return _render(request,
        "admin/dashboard.html",
        _ctx(request, db, competitions=competitions, today=date.today()),
    )


# ── 공모전 CRUD ───────────────────────────────────────────────────────────────

@app.get("/admin/add", response_class=HTMLResponse)
async def admin_add_page(request: Request, db: Session = Depends(get_db)):
    if r := _admin_redirect(request):
        return r
    return _render(request,
        "admin/form.html",
        _ctx(request, db, comp=None, tags=TAGS, action="/admin/add", title="공모전 추가"),
    )


@app.post("/admin/add")
async def admin_add(
    request: Request,
    title: str = Form(...),
    organizer: str = Form(""),
    tags: List[str] = Form(default=[]),
    start_date: Optional[str] = Form(None),
    deadline: str = Form(...),
    announcement_date: Optional[str] = Form(None),
    prize: str = Form(""),
    link: str = Form(""),
    description: str = Form(""),
    is_featured: bool = Form(False),
    max_members: Optional[str] = Form(None),
    comp_image_path: Optional[str] = Form(None),
    comp_image: Optional[UploadFile] = File(None),
    files: List[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    if r := _admin_redirect(request):
        return r
    image = await _save_image(comp_image) or comp_image_path or None
    comp = Competition(
        title=title, organizer=organizer,
        tags=json.dumps(tags, ensure_ascii=False),
        start_date=date.fromisoformat(start_date) if start_date else None,
        deadline=date.fromisoformat(deadline),
        announcement_date=date.fromisoformat(announcement_date) if announcement_date else None,
        prize=prize, link=link, description=description,
        image=image, max_members=_optional_int(max_members, "최대 팀 인원"), is_featured=is_featured,
        files=json.dumps(await _save_files(files), ensure_ascii=False),
    )
    db.add(comp)
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/admin/edit/{comp_id}", response_class=HTMLResponse)
async def admin_edit_page(request: Request, comp_id: int, db: Session = Depends(get_db)):
    if r := _admin_redirect(request):
        return r
    comp = db.query(Competition).filter(Competition.id == comp_id).first()
    if not comp:
        raise HTTPException(status_code=404)
    comp.tags_list = _from_json(comp.tags)
    comp.files_list = _from_json(comp.files)
    return _render(request,
        "admin/form.html",
        _ctx(request, db, comp=comp, tags=TAGS, action=f"/admin/edit/{comp_id}", title="공모전 수정"),
    )


@app.post("/admin/edit/{comp_id}")
async def admin_edit(
    request: Request, comp_id: int,
    title: str = Form(...), organizer: str = Form(""),
    tags: List[str] = Form(default=[]),
    start_date: Optional[str] = Form(None), deadline: str = Form(...),
    announcement_date: Optional[str] = Form(None),
    prize: str = Form(""), link: str = Form(""), description: str = Form(""),
    is_featured: bool = Form(False), max_members: Optional[str] = Form(None),
    comp_image_path: Optional[str] = Form(None),
    comp_image: Optional[UploadFile] = File(None),
    files: List[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    if r := _admin_redirect(request):
        return r
    comp = db.query(Competition).filter(Competition.id == comp_id).first()
    if not comp:
        raise HTTPException(status_code=404)

    new_image = await _save_image(comp_image)
    existing_files = _from_json(comp.files)
    comp.title = title; comp.organizer = organizer
    comp.tags = json.dumps(tags, ensure_ascii=False)
    comp.start_date = date.fromisoformat(start_date) if start_date else None
    comp.deadline = date.fromisoformat(deadline)
    comp.announcement_date = date.fromisoformat(announcement_date) if announcement_date else None
    comp.prize = prize; comp.link = link; comp.description = description
    comp.is_featured = is_featured; comp.max_members = _optional_int(max_members, "최대 팀 인원")
    comp.image = new_image or comp_image_path or comp.image
    comp.files = json.dumps(existing_files + await _save_files(files), ensure_ascii=False)
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete/{comp_id}")
async def admin_delete(request: Request, comp_id: int, db: Session = Depends(get_db)):
    if r := _admin_redirect(request):
        return r
    comp = db.query(Competition).filter(Competition.id == comp_id).first()
    if comp:
        for item in _from_json(comp.files):
            try:
                (UPLOAD_DIR / item["path"]).unlink(missing_ok=True)
            except OSError:
                pass
        db.delete(comp)
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete-file/{comp_id}")
async def delete_file(request: Request, comp_id: int, filename: str = Form(...), db: Session = Depends(get_db)):
    if r := _admin_redirect(request):
        return r
    comp = db.query(Competition).filter(Competition.id == comp_id).first()
    if comp:
        updated = [f for f in _from_json(comp.files) if f.get("path") != filename]
        comp.files = json.dumps(updated, ensure_ascii=False)
        db.commit()
        try:
            (UPLOAD_DIR / filename).unlink(missing_ok=True)
        except OSError:
            pass
    return RedirectResponse(url=f"/admin/edit/{comp_id}", status_code=303)


# ── GPT 파싱 API ──────────────────────────────────────────────────────────────

@app.post("/admin/api/parse")
async def api_parse(request: Request, text: str = Form(...)):
    if not _is_admin(request):
        raise HTTPException(status_code=401)
    try:
        return JSONResponse(await parse_text(text))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/admin/api/parse-image")
async def api_parse_image(request: Request, image: UploadFile = File(...)):
    if not _is_admin(request):
        raise HTTPException(status_code=401)
    try:
        data = await image.read()
        ext = Path(image.filename).suffix.lower() if image.filename else ".jpg"
        stored_name = f"{uuid.uuid4().hex}{ext}"
        (UPLOAD_DIR / stored_name).write_bytes(data)
        result = await parse_image_file(data, image.content_type)
        result["_image_path"] = stored_name
        return JSONResponse(result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/admin/api/parse-document")
async def api_parse_document(request: Request, document: UploadFile = File(...)):
    if not _is_admin(request):
        raise HTTPException(status_code=401)
    try:
        data = await document.read()
        result = await parse_document_file(data, document.filename or "file.pdf")
        return JSONResponse(result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── 회원 관리 ─────────────────────────────────────────────────────────────────

@app.get("/admin/members", response_class=HTMLResponse)
async def admin_members(request: Request, db: Session = Depends(get_db)):
    if r := _admin_redirect(request):
        return r
    members = db.query(Member).order_by(Member.created_at.asc()).all()
    return _render(request, "admin/members.html", _ctx(request, db, members=members, now=datetime.now()))


@app.post("/admin/members/{member_id}/set-role")
async def admin_set_role(request: Request, member_id: int, role: str = Form(...), db: Session = Depends(get_db)):
    if r := _admin_redirect(request):
        return r
    m = db.query(Member).filter(Member.id == member_id).first()
    if m and role in ("member", "sub_admin"):
        m.role = role
        db.commit()
    return RedirectResponse(url="/admin/members", status_code=303)


@app.post("/admin/members/{member_id}/delete")
async def admin_delete_member(request: Request, member_id: int, db: Session = Depends(get_db)):
    if r := _admin_redirect(request):
        return r
    m = db.query(Member).filter(Member.id == member_id).first()
    if m:
        db.delete(m)
        db.commit()
    return RedirectResponse(url="/admin/members", status_code=303)


@app.post("/admin/members/{member_id}/mute-comments")
async def admin_mute_member_comments(
    request: Request,
    member_id: int,
    duration_minutes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    if r := _admin_redirect(request):
        return r
    member = db.query(Member).filter(Member.id == member_id).first()
    if member:
        minutes = _optional_int(duration_minutes, "댓글 금지 시간")
        member.comment_muted_until = (datetime.now() + timedelta(minutes=minutes)) if minutes and minutes > 0 else None
        db.commit()
    return RedirectResponse(url="/admin/members", status_code=303)


# ── 초대 코드 ─────────────────────────────────────────────────────────────────

@app.get("/admin/invite-codes", response_class=HTMLResponse)
async def admin_invite_codes(request: Request, db: Session = Depends(get_db)):
    if r := _admin_redirect(request):
        return r
    codes = db.query(InviteCode).order_by(InviteCode.created_at.desc()).all()
    code_ids = [c.id for c in codes]
    logs = (
        db.query(InviteCodeUseLog)
        .filter(InviteCodeUseLog.invite_code_id.in_(code_ids))
        .order_by(InviteCodeUseLog.used_at.desc())
        .all()
    ) if code_ids else []
    logs_by_code: dict[int, list[InviteCodeUseLog]] = defaultdict(list)
    for log in logs:
        logs_by_code[log.invite_code_id].append(log)
    used_ids = [c.used_by_member_id for c in codes if c.used_by_member_id] + [log.member_id for log in logs if log.member_id]
    members_map = {}
    if used_ids:
        for m in db.query(Member).filter(Member.id.in_(used_ids)).all():
            members_map[m.id] = m.activity_name
    return _render(request,
        "admin/invite_codes.html",
        _ctx(request, db, codes=codes, logs_by_code=logs_by_code, members_map=members_map, now=datetime.now()),
    )


@app.post("/admin/invite-codes/create")
async def admin_create_invite_code(
    request: Request,
    note: str = Form(""),
    code_type: str = Form("personal"),
    max_uses: Optional[str] = Form(None),
    valid_days: Optional[str] = Form(None),
    expires_at: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    if r := _admin_redirect(request):
        return r
    code_type = code_type if code_type in ("personal", "group") else "personal"
    parsed_max_uses = 1 if code_type == "personal" else _optional_int(max_uses, "최대 사용 인원")
    if code_type == "group" and (not parsed_max_uses or parsed_max_uses < 1):
        raise HTTPException(status_code=400, detail="단체 초대 코드는 최대 사용 인원을 1명 이상으로 입력해야 합니다.")
    db.add(InviteCode(
        code=secrets.token_urlsafe(12),
        note=note.strip(),
        code_type=code_type,
        max_uses=parsed_max_uses,
        expires_at=_parse_expiry(valid_days, expires_at),
        use_count=0,
        is_active=True,
    ))
    db.commit()
    return RedirectResponse(url="/admin/invite-codes", status_code=303)


@app.post("/admin/invite-codes/delete/{code_id}")
async def admin_delete_invite_code(request: Request, code_id: int, db: Session = Depends(get_db)):
    if r := _admin_redirect(request):
        return r
    code = db.query(InviteCode).filter(InviteCode.id == code_id).first()
    if code:
        db.delete(code)
        db.commit()
    return RedirectResponse(url="/admin/invite-codes", status_code=303)


@app.post("/admin/invite-codes/logs/{log_id}/kick")
async def admin_kick_invite_member(request: Request, log_id: int, db: Session = Depends(get_db)):
    if r := _admin_redirect(request):
        return r
    log = db.query(InviteCodeUseLog).filter(InviteCodeUseLog.id == log_id).first()
    if not log or log.revoked_at:
        return RedirectResponse(url="/admin/invite-codes", status_code=303)
    code = db.query(InviteCode).filter(InviteCode.id == log.invite_code_id).first()
    member = db.query(Member).filter(Member.id == log.member_id).first() if log.member_id else None
    if member:
        db.delete(member)
    log.revoked_at = datetime.now()
    log.revoked_by = "admin"
    if code and code.code_type == "group" and (code.use_count or 0) > 0:
        code.use_count -= 1
    db.commit()
    return RedirectResponse(url="/admin/invite-codes", status_code=303)


# ════════════════════════════════════════════════════════════════════════════
#  회원 — 가입 / 로그인 / 로그아웃 / 프로필
# ════════════════════════════════════════════════════════════════════════════

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, db: Session = Depends(get_db)):
    if _current_member(request, db):
        return RedirectResponse(url="/", status_code=303)
    return _render(request, "register.html", _ctx(request, db, error=None))


@app.post("/register")
async def register(
    request: Request,
    invite_code: str = Form(...), activity_name: str = Form(...), real_name: str = Form(...),
    student_id: str = Form(""), phone: str = Form(""),
    password: str = Form(...), bio: str = Form(""),
    db: Session = Depends(get_db),
):
    def err(msg):
        return _render(request, "register.html", _ctx(request, db, error=msg), status_code=400)

    code_obj = db.query(InviteCode).filter(InviteCode.code == invite_code.strip()).first()
    if not code_obj:
        return err("초대 코드가 올바르지 않습니다.")
    if not code_obj.is_active:
        return err("비활성화된 초대 코드입니다.")
    if code_obj.expires_at and datetime.now() > code_obj.expires_at:
        return err("만료된 초대 코드입니다.")
    code_type = code_obj.code_type or "personal"
    if code_type == "personal" and code_obj.used_by_member_id:
        return err("이미 사용된 개인 초대 코드입니다.")
    if code_type == "group" and code_obj.max_uses and (code_obj.use_count or 0) >= code_obj.max_uses:
        return err("단체 초대 코드 사용 가능 인원이 모두 찼습니다.")
    if db.query(Member).filter(Member.activity_name == activity_name.strip()).first():
        return err("이미 사용 중인 활동명입니다.")
    if len(password) < 6:
        return err("비밀번호는 최소 6자 이상이어야 합니다.")

    member = Member(
        activity_name=activity_name.strip(), real_name=real_name.strip(),
        student_id=student_id.strip(), phone=phone.strip(),
        password_hash=hash_password(password), bio=bio.strip(),
        invite_code_used=invite_code.strip(),
    )
    db.add(member)
    db.flush()
    db.add(InviteCodeUseLog(
        invite_code_id=code_obj.id,
        member_id=member.id,
        activity_name=member.activity_name,
        real_name=member.real_name,
    ))
    code_obj.use_count = (code_obj.use_count or 0) + 1
    if code_type == "personal":
        code_obj.used_by_member_id = member.id
        code_obj.is_active = False
    db.commit()

    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie("member_token", create_member_token(member.id), httponly=True, max_age=604800, samesite="lax")
    return resp


@app.get("/member/login", response_class=HTMLResponse)
async def member_login_page(request: Request, db: Session = Depends(get_db)):
    if _current_member(request, db):
        return RedirectResponse(url="/", status_code=303)
    return _render(request, "member_login.html", _ctx(request, db, error=None))


@app.post("/member/login")
async def member_login(request: Request, activity_name: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    m = db.query(Member).filter(Member.activity_name == activity_name.strip()).first()
    if not m or not verify_password(password, m.password_hash):
        return _render(request,
            "member_login.html",
            _ctx(request, db, error="활동명 또는 비밀번호가 올바르지 않습니다."),
            status_code=401,
        )
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie("member_token", create_member_token(m.id), httponly=True, max_age=604800, samesite="lax")
    return resp


@app.get("/member/logout")
async def member_logout():
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie("member_token")
    return resp


@app.get("/profile/me", response_class=HTMLResponse)
async def profile_me(request: Request, db: Session = Depends(get_db)):
    cm = _current_member(request, db)
    if not cm:
        return RedirectResponse(url="/member/login", status_code=303)
    return RedirectResponse(url=f"/profile/{cm.activity_name}", status_code=303)


@app.get("/profile/{activity_name}", response_class=HTMLResponse)
async def profile_view(request: Request, activity_name: str, db: Session = Depends(get_db)):
    target = db.query(Member).filter(Member.activity_name == activity_name).first()
    if not target:
        raise HTTPException(status_code=404, detail="회원을 찾을 수 없습니다.")
    cm = _current_member(request, db)

    # 포트폴리오: 이 회원이 참여한 팀원 레코드 (최신순)
    team_rows = (
        db.query(TeamMember)
        .filter(TeamMember.member_id == target.id)
        .order_by(TeamMember.created_at.desc())
        .all()
    )
    comp_ids = list({t.competition_id for t in team_rows})
    comps_map: dict = {}
    if comp_ids:
        for c in db.query(Competition).filter(Competition.id.in_(comp_ids)).all():
            comps_map[c.id] = c
    total     = len(team_rows)
    submitted = sum(1 for t in team_rows if t.is_participant)
    awarded   = sum(1 for t in team_rows if t.award_rank)

    return _render(request,
        "profile.html",
        _ctx(request, db, target=target, is_own=bool(cm and cm.id == target.id),
             team_rows=team_rows, comps_map=comps_map,
             stats={"total": total, "submitted": submitted, "awarded": awarded}),
    )


@app.get("/profile/edit/me", response_class=HTMLResponse)
async def profile_edit_page(request: Request, db: Session = Depends(get_db)):
    cm = _current_member(request, db)
    if not cm:
        return RedirectResponse(url="/member/login", status_code=303)
    return _render(request, "profile_edit.html", _ctx(request, db, member=cm, error=None))


@app.post("/profile/edit/me")
async def profile_edit(
    request: Request,
    bio: str = Form(""), real_name: str = Form(...), phone: str = Form(""),
    new_password: str = Form(""), current_password: str = Form(...),
    profile_image: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    cm = _current_member(request, db)
    if not cm:
        return RedirectResponse(url="/member/login", status_code=303)
    if not verify_password(current_password, cm.password_hash):
        return _render(request, "profile_edit.html", _ctx(request, db, member=cm, error="현재 비밀번호가 올바르지 않습니다."), status_code=400)

    cm.bio = bio.strip(); cm.real_name = real_name.strip(); cm.phone = phone.strip()
    new_img = await _save_image(profile_image)
    if new_img:
        cm.profile_image = new_img
    if new_password:
        if len(new_password) < 6:
            return _render(request, "profile_edit.html", _ctx(request, db, member=cm, error="새 비밀번호는 최소 6자 이상이어야 합니다."), status_code=400)
        cm.password_hash = hash_password(new_password)
    db.commit()
    return RedirectResponse(url=f"/profile/{cm.activity_name}", status_code=303)


# ════════════════════════════════════════════════════════════════════════════
#  팀 구성
# ════════════════════════════════════════════════════════════════════════════

@app.post("/competition/{comp_id}/team/create")
async def create_team(
    request: Request, comp_id: int,
    team_name: str = Form(...),
    team_desc: str = Form(""),
    team_requirements: str = Form(""),
    nickname: str = Form(...),
    password: str = Form(...),
    role: str = Form("기타"),
    memo: str = Form(""),
    db: Session = Depends(get_db),
):
    comp = db.query(Competition).filter(Competition.id == comp_id).first()
    if not comp:
        raise HTTPException(status_code=404)
    if date.today() > comp.deadline:
        raise HTTPException(status_code=400, detail="마감된 공모전입니다.")
    team_name = team_name.strip()
    if not team_name:
        raise HTTPException(status_code=400, detail="팀 이름을 입력하세요.")
    if db.query(Team).filter(Team.competition_id == comp_id, Team.name == team_name).first():
        raise HTTPException(status_code=400, detail="같은 이름의 팀이 이미 있습니다.")
    try:
        team = Team(
            competition_id=comp_id,
            name=team_name,
            description=(team_desc or "").strip(),
            requirements=(team_requirements or "").strip(),
        )
        db.add(team)
        db.commit()
        db.refresh(team)
        cm = _current_member(request, db)
        db.add(TeamMember(
            team_id=team.id,
            competition_id=comp_id,
            nickname=nickname.strip(),
            password_hash=_hash_pw(password),
            role=role if role in ROLES else "기타",
            memo=(memo or "").strip(),
            is_leader=True,
            member_id=cm.id if cm else None,
        ))
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"팀 생성 오류: {exc}")
    return RedirectResponse(url=f"/competition/{comp_id}#team", status_code=303)


@app.post("/competition/{comp_id}/team/{team_id}/join")
async def team_join(
    request: Request, comp_id: int, team_id: int,
    nickname: str = Form(...), password: str = Form(...),
    role: str = Form("기타"), memo: str = Form(""),
    db: Session = Depends(get_db),
):
    comp = db.query(Competition).filter(Competition.id == comp_id).first()
    if not comp:
        raise HTTPException(status_code=404)
    team = db.query(Team).filter(Team.id == team_id, Team.competition_id == comp_id).first()
    if not team:
        raise HTTPException(status_code=404)
    members = db.query(TeamMember).filter(TeamMember.team_id == team_id).all()
    if comp.max_members and len(members) >= comp.max_members:
        raise HTTPException(status_code=400, detail="팀 인원이 가득 찼습니다.")
    if db.query(TeamMember).filter(TeamMember.team_id == team_id, TeamMember.nickname == nickname).first():
        raise HTTPException(status_code=400, detail="이미 같은 닉네임으로 참여 중입니다.")
    cm = _current_member(request, db)
    db.add(TeamMember(
        team_id=team_id,
        competition_id=comp_id,
        nickname=nickname,
        password_hash=_hash_pw(password),
        role=role if role in ROLES else "기타",
        memo=memo,
        is_leader=len(members) == 0,
        member_id=cm.id if cm else None,
    ))
    db.commit()
    return RedirectResponse(url=f"/competition/{comp_id}#team", status_code=303)


@app.post("/competition/{comp_id}/team/{team_id}/leave/{member_id}")
async def team_leave(
    request: Request, comp_id: int, team_id: int, member_id: int,
    nickname: str = Form(...), password: str = Form(...),
    db: Session = Depends(get_db),
):
    member = db.query(TeamMember).filter(
        TeamMember.id == member_id,
        TeamMember.team_id == team_id,
    ).first()
    if not member:
        raise HTTPException(status_code=404)
    if not _is_privileged(request, db):
        if member.nickname != nickname or not _verify_pw(password, member.password_hash):
            raise HTTPException(status_code=400, detail="닉네임 또는 비밀번호가 올바르지 않습니다.")
    was_leader = member.is_leader
    db.delete(member)
    db.commit()
    # 남은 팀원 있으면 리더 재배정, 없으면 팀 삭제
    remaining = db.query(TeamMember).filter(TeamMember.team_id == team_id).order_by(TeamMember.created_at.asc()).all()
    if not remaining:
        team = db.query(Team).filter(Team.id == team_id).first()
        if team:
            db.delete(team)
            db.commit()
    elif was_leader:
        remaining[0].is_leader = True
        db.commit()
    return RedirectResponse(url=f"/competition/{comp_id}#team", status_code=303)


@app.post("/admin/competition/{comp_id}/team/{team_id}/set-leader/{member_id}")
async def set_leader(request: Request, comp_id: int, team_id: int, member_id: int, db: Session = Depends(get_db)):
    if not _is_privileged(request, db):
        raise HTTPException(status_code=403)
    db.query(TeamMember).filter(TeamMember.team_id == team_id, TeamMember.is_leader.is_(True)).update({"is_leader": False})
    m = db.query(TeamMember).filter(TeamMember.id == member_id).first()
    if m:
        m.is_leader = True
    db.commit()
    return RedirectResponse(url=f"/competition/{comp_id}#team", status_code=303)


# ── 어드민: 공모전별 참여자·수상 관리 ─────────────────────────────────────────────

@app.get("/admin/competition/{comp_id}/members", response_class=HTMLResponse)
async def admin_comp_members(request: Request, comp_id: int, db: Session = Depends(get_db)):
    if r := _admin_redirect(request):
        return r
    comp = db.query(Competition).filter(Competition.id == comp_id).first()
    if not comp:
        raise HTTPException(status_code=404)
    teams = db.query(Team).filter(Team.competition_id == comp_id).order_by(Team.created_at.asc()).all()
    team_ids = [t.id for t in teams]
    all_tm = db.query(TeamMember).filter(TeamMember.team_id.in_(team_ids)).order_by(TeamMember.created_at.asc()).all() if team_ids else []
    tm_by_team: dict = {}
    for tm in all_tm:
        tm_by_team.setdefault(tm.team_id, []).append(tm)
    for t in teams:
        t.members = tm_by_team.get(t.id, [])
    member_ids = [tm.member_id for tm in all_tm if tm.member_id]
    members_map = {}
    if member_ids:
        for m in db.query(Member).filter(Member.id.in_(member_ids)).all():
            members_map[m.id] = m
    return _render(request,
        "admin/comp_members.html",
        _ctx(request, db, comp=comp, teams=teams, members_map=members_map, award_ranks=AWARD_RANKS),
    )


@app.post("/admin/competition/{comp_id}/members/{tm_id}/award")
async def admin_set_award(
    request: Request, comp_id: int, tm_id: int,
    award_rank: str = Form(""),
    award_prize: str = Form(""),
    award_note: str = Form(""),
    db: Session = Depends(get_db),
):
    """팀원 한 명의 수상 정보 저장"""
    if r := _admin_redirect(request):
        return r
    tm = db.query(TeamMember).filter(TeamMember.id == tm_id).first()
    if not tm:
        raise HTTPException(status_code=404)
    tm.award_rank  = award_rank if award_rank in AWARD_RANKS else None
    tm.award_prize = award_prize.strip()
    tm.award_note  = award_note.strip()
    db.commit()
    return RedirectResponse(url=f"/admin/competition/{comp_id}/members", status_code=303)


@app.post("/competition/{comp_id}/team/{team_id}/submit")
async def record_submission(
    request: Request, comp_id: int, team_id: int,
    participant_ids: List[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    if not _is_privileged(request, db):
        raise HTTPException(status_code=403)
    team = db.query(Team).filter(Team.id == team_id, Team.competition_id == comp_id).first()
    if not team:
        raise HTTPException(status_code=404)
    comp = db.query(Competition).filter(Competition.id == comp_id).first()
    today = date.today()
    if not (comp.deadline < today <= comp.deadline + timedelta(days=7)):
        raise HTTPException(status_code=400, detail="제출 기록 기간이 아닙니다.")
    db.query(TeamMember).filter(TeamMember.team_id == team_id).update({"is_participant": False})
    if participant_ids:
        db.query(TeamMember).filter(TeamMember.team_id == team_id, TeamMember.id.in_(participant_ids)).update({"is_participant": True})
    team.submitted = True
    team.submitted_at = datetime.now()
    db.commit()
    return RedirectResponse(url=f"/competition/{comp_id}#team", status_code=303)


# ════════════════════════════════════════════════════════════════════════════
#  게시판
# ════════════════════════════════════════════════════════════════════════════

@app.get("/board/{board}", response_class=HTMLResponse)
async def board_list(
    request: Request, board: str,
    page: int = Query(1, ge=1),
    q: str = "",
    db: Session = Depends(get_db),
):
    if board not in BOARDS:
        raise HTTPException(status_code=404)

    page_size = 20
    post_query = db.query(Post).filter(Post.board == board)
    if q:
        compact_q = _compact_text(q)
        post_query = post_query.filter(
            or_(
                _compact_column(Post.title).contains(compact_q),
                _compact_column(Post.content).contains(compact_q),
            )
        )
    total = post_query.with_entities(func.count(Post.id)).scalar()
    posts = (
        post_query
        .order_by(Post.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    # 작성자 매핑
    author_ids = list({p.author_id for p in posts})
    authors = _member_map(db, author_ids)

    # 댓글 수 / 좋아요 수
    post_ids = [p.id for p in posts]
    comment_counts = dict(
        db.query(Comment.post_id, func.count(Comment.id))
        .filter(Comment.post_id.in_(post_ids), Comment.parent_id.is_(None))
        .group_by(Comment.post_id).all()
    ) if post_ids else {}
    like_counts = dict(
        db.query(PostLike.post_id, func.count(PostLike.id))
        .filter(PostLike.post_id.in_(post_ids))
        .group_by(PostLike.post_id).all()
    ) if post_ids else {}

    for p in posts:
        p.author = authors.get(p.author_id)
        p.comment_count = comment_counts.get(p.id, 0)
        p.like_count = like_counts.get(p.id, 0)

    total_pages = max(1, (total + page_size - 1) // page_size)

    return _render(request,
        "board/list.html",
        _ctx(request, db,
             board=board, board_name=BOARDS[board],
             posts=posts, page=page, total_pages=total_pages, query=q),
    )


@app.get("/board/{board}/new", response_class=HTMLResponse)
async def board_new_page(request: Request, board: str, db: Session = Depends(get_db)):
    if board not in BOARDS:
        raise HTTPException(status_code=404)
    cm = _current_member(request, db)
    if not cm:
        return RedirectResponse(url="/member/login", status_code=303)
    return _render(request,
        "board/post_new.html",
        _ctx(request, db, board=board, board_name=BOARDS[board], error=None),
    )


@app.post("/board/{board}/new")
async def board_new_post(
    request: Request, board: str,
    title: str = Form(...), content: str = Form(""),
    images: List[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    if board not in BOARDS:
        raise HTTPException(status_code=404)
    cm = _current_member(request, db)
    if not cm:
        raise HTTPException(status_code=401)

    saved_images = await _save_images(images)
    post = Post(
        board=board, title=title.strip(), content=content,
        author_id=cm.id,
        images=json.dumps(saved_images, ensure_ascii=False),
    )
    db.add(post)
    db.commit()
    return RedirectResponse(url=f"/board/{board}/post/{post.id}", status_code=303)


@app.get("/board/{board}/post/{post_id}", response_class=HTMLResponse)
async def board_post_detail(request: Request, board: str, post_id: int, db: Session = Depends(get_db)):
    if board not in BOARDS:
        raise HTTPException(status_code=404)
    post = db.query(Post).filter(Post.id == post_id, Post.board == board).first()
    if not post:
        raise HTTPException(status_code=404)

    post.view_count += 1
    db.commit()
    db.refresh(post)

    # 작성자 정보
    author = db.query(Member).filter(Member.id == post.author_id).first()

    # 댓글 (계층 구조)
    all_comments = db.query(Comment).filter(Comment.post_id == post_id).order_by(Comment.created_at.asc()).all()

    # 댓글 작성자 정보
    c_author_ids = list({c.author_id for c in all_comments})
    c_authors = _member_map(db, c_author_ids)

    # 좋아요
    cm = _current_member(request, db)
    post_likes = db.query(func.count(PostLike.id)).filter(PostLike.post_id == post_id).scalar()
    user_liked_post = bool(cm and db.query(PostLike).filter(PostLike.post_id == post_id, PostLike.member_id == cm.id).first())

    # 댓글 좋아요
    c_ids = [c.id for c in all_comments]
    c_like_counts = dict(
        db.query(CommentLike.comment_id, func.count(CommentLike.id))
        .filter(CommentLike.comment_id.in_(c_ids))
        .group_by(CommentLike.comment_id).all()
    ) if c_ids else {}
    user_liked_comments = set()
    if cm and c_ids:
        liked = db.query(CommentLike.comment_id).filter(CommentLike.member_id == cm.id, CommentLike.comment_id.in_(c_ids)).all()
        user_liked_comments = {r[0] for r in liked}

    for c in all_comments:
        c.author = c_authors.get(c.author_id)
        c.like_count = c_like_counts.get(c.id, 0)
        c.user_liked = c.id in user_liked_comments

    # 계층 정리: top_comments + replies 매핑
    top_comments = [c for c in all_comments if c.parent_id is None]
    replies = defaultdict(list)
    for c in all_comments:
        if c.parent_id is not None:
            replies[c.parent_id].append(c)

    return _render(request,
        "board/post_detail.html",
        _ctx(request, db,
             board=board, board_name=BOARDS[board],
             post=post, author=author,
             images=_from_json(post.images),
             top_comments=top_comments, replies=dict(replies),
             post_likes=post_likes, user_liked_post=user_liked_post),
    )


@app.post("/board/{board}/post/{post_id}/delete")
async def board_delete_post(request: Request, board: str, post_id: int, db: Session = Depends(get_db)):
    post = db.query(Post).filter(Post.id == post_id, Post.board == board).first()
    if not post:
        raise HTTPException(status_code=404)
    cm = _current_member(request, db)
    is_author = cm and cm.id == post.author_id
    if not is_author and not _is_privileged(request, db):
        raise HTTPException(status_code=403)
    # 이미지 파일 삭제
    for img in _from_json(post.images):
        try:
            (UPLOAD_DIR / img).unlink(missing_ok=True)
        except OSError:
            pass
    db.delete(post)
    db.commit()
    return RedirectResponse(url=f"/board/{board}", status_code=303)


@app.post("/board/{board}/post/{post_id}/like")
async def board_like_post(request: Request, board: str, post_id: int, db: Session = Depends(get_db)):
    cm = _current_member(request, db)
    if not cm:
        raise HTTPException(status_code=401)
    existing = db.query(PostLike).filter(PostLike.post_id == post_id, PostLike.member_id == cm.id).first()
    if existing:
        db.delete(existing)
    else:
        db.add(PostLike(post_id=post_id, member_id=cm.id))
    db.commit()
    return RedirectResponse(url=f"/board/{board}/post/{post_id}", status_code=303)


@app.post("/board/{board}/post/{post_id}/comment")
async def board_add_comment(
    request: Request, board: str, post_id: int,
    content: str = Form(...), parent_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
):
    cm = _current_member(request, db)
    if not cm:
        raise HTTPException(status_code=401)
    if _is_comment_muted(cm):
        raise HTTPException(status_code=403, detail=f"댓글 작성이 {cm.comment_muted_until.strftime('%Y.%m.%d %H:%M')}까지 제한되었습니다.")
    if not content.strip():
        raise HTTPException(status_code=400, detail="댓글 내용을 입력하세요.")
    db.add(Comment(post_id=post_id, parent_id=parent_id, author_id=cm.id, content=content.strip()))
    db.commit()
    return RedirectResponse(url=f"/board/{board}/post/{post_id}#comments", status_code=303)


@app.post("/board/{board}/comment/{comment_id}/delete")
async def board_delete_comment(request: Request, board: str, comment_id: int, db: Session = Depends(get_db)):
    comment = db.query(Comment).filter(Comment.id == comment_id).first()
    if not comment:
        raise HTTPException(status_code=404)
    cm = _current_member(request, db)
    is_author = cm and cm.id == comment.author_id
    if not is_author and not _is_privileged(request, db):
        raise HTTPException(status_code=403)
    post_id = comment.post_id
    db.delete(comment)
    db.commit()
    return RedirectResponse(url=f"/board/{board}/post/{post_id}#comments", status_code=303)


@app.post("/board/{board}/comment/{comment_id}/like")
async def board_like_comment(request: Request, board: str, comment_id: int, db: Session = Depends(get_db)):
    cm = _current_member(request, db)
    if not cm:
        raise HTTPException(status_code=401)
    comment = db.query(Comment).filter(Comment.id == comment_id).first()
    if not comment:
        raise HTTPException(status_code=404)
    existing = db.query(CommentLike).filter(CommentLike.comment_id == comment_id, CommentLike.member_id == cm.id).first()
    if existing:
        db.delete(existing)
    else:
        db.add(CommentLike(comment_id=comment_id, member_id=cm.id))
    db.commit()
    return RedirectResponse(url=f"/board/{board}/post/{comment.post_id}#comments", status_code=303)


# ════════════════════════════════════════════════════════════════════════════
#  채팅
# ════════════════════════════════════════════════════════════════════════════

class _RoomManager:
    def __init__(self):
        self.connections: dict[int, dict] = defaultdict(dict)

    async def join(self, room_id: int, ws: WebSocket, member: Member):
        await ws.accept()
        self.connections[room_id][ws] = {
            "id": member.id,
            "name": member.activity_name,
            "profile_image": member.profile_image,
        }

    def leave(self, room_id: int, ws: WebSocket):
        self.connections[room_id].pop(ws, None)

    def online(self, room_id: int) -> list[dict]:
        seen = {}
        for item in self.connections.get(room_id, {}).values():
            seen[item["id"]] = item
        return sorted(seen.values(), key=lambda row: row["name"])

    async def broadcast(self, room_id: int, msg: dict):
        dead = set()
        for ws in list(self.connections.get(room_id, {}).keys()):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.connections[room_id].pop(ws, None)


async def _broadcast_room_state(room_id: int, db: Session):
    members_payload = []
    for row in _room_members(db, room_id):
        if row.member:
            members_payload.append({
                "id": row.member_id,
                "name": row.member.activity_name,
                "role": row.role,
                "muted_until": row.muted_until.strftime("%Y.%m.%d %H:%M") if row.muted_until else "",
            })
    await _room_mgr.broadcast(room_id, {
        "type": "presence",
        "online": _room_mgr.online(room_id),
        "members": members_payload,
    })


_room_mgr = _RoomManager()


@app.get("/chat", response_class=HTMLResponse)
async def chat_list(
    request: Request,
    q: str = "",
    sort: str = "created",
    order: str = "desc",
    db: Session = Depends(get_db),
):
    cm = _current_member(request, db)
    if not cm:
        return RedirectResponse(url="/member/login", status_code=303)

    query = db.query(ChatRoom)
    if q:
        compact_q = _compact_text(q)
        query = query.filter(
            or_(
                _compact_column(ChatRoom.name).contains(compact_q),
                _compact_column(ChatRoom.description).contains(compact_q),
            )
        )
    rooms = query.all()
    room_ids = [room.id for room in rooms]
    member_counts = dict(
        db.query(ChatRoomMember.room_id, func.count(ChatRoomMember.id))
        .filter(ChatRoomMember.room_id.in_(room_ids))
        .group_by(ChatRoomMember.room_id)
        .all()
    ) if room_ids else {}
    creator_ids = list({r.created_by_id for r in rooms})
    creators = _member_map(db, creator_ids)
    for r in rooms:
        r.creator = creators.get(r.created_by_id)
        r.online_count = len(_room_mgr.online(r.id))
        r.member_count = member_counts.get(r.id, 0)
        r.has_password = bool(r.password_hash)

    reverse = order != "asc"
    if sort == "name":
        rooms.sort(key=lambda room: room.name.lower(), reverse=reverse)
    elif sort == "members":
        rooms.sort(key=lambda room: (room.member_count, room.name.lower()), reverse=reverse)
    else:
        rooms.sort(key=lambda room: room.created_at, reverse=reverse)

    return _render(request,
        "chat/list.html",
        _ctx(request, db, rooms=rooms, query=q, current_sort=sort, current_order=order),
    )


@app.post("/chat/create")
async def chat_create(
    request: Request,
    name: str = Form(...), description: str = Form(""), password: str = Form(""),
    db: Session = Depends(get_db),
):
    cm = _current_member(request, db)
    if not cm:
        raise HTTPException(status_code=401)
    room = ChatRoom(
        name=name.strip(), description=description.strip(),
        password_hash=_hash_pw(password) if password else None,
        created_by_id=cm.id,
    )
    db.add(room)
    db.flush()
    db.add(ChatRoomMember(room_id=room.id, member_id=cm.id, role="owner"))
    db.commit()
    return RedirectResponse(url=f"/chat/{room.id}", status_code=303)


@app.post("/chat/{room_id}/delete")
async def chat_delete_room(request: Request, room_id: int, db: Session = Depends(get_db)):
    if not _is_privileged(request, db):
        raise HTTPException(status_code=403)
    room = db.query(ChatRoom).filter(ChatRoom.id == room_id).first()
    if room:
        db.delete(room)
        db.commit()
    return RedirectResponse(url="/chat", status_code=303)


@app.post("/chat/{room_id}/leave")
async def chat_leave_room(request: Request, room_id: int, db: Session = Depends(get_db)):
    cm = _current_member(request, db)
    if not cm:
        return RedirectResponse(url="/member/login", status_code=303)
    room = db.query(ChatRoom).filter(ChatRoom.id == room_id).first()
    row = _chat_member(db, room_id, cm.id)
    if not room or not row:
        return RedirectResponse(url="/chat", status_code=303)
    was_owner = row.role == "owner"
    db.delete(row)
    db.flush()
    if was_owner:
        next_owner = (
            db.query(ChatRoomMember)
            .filter(ChatRoomMember.room_id == room_id, ChatRoomMember.role == "co_owner")
            .order_by(ChatRoomMember.joined_at.asc())
            .first()
        ) or (
            db.query(ChatRoomMember)
            .filter(ChatRoomMember.room_id == room_id)
            .order_by(ChatRoomMember.joined_at.asc())
            .first()
        )
        if next_owner:
            next_owner.role = "owner"
            room.created_by_id = next_owner.member_id
        else:
            db.delete(room)
    db.commit()
    return RedirectResponse(url="/chat", status_code=303)


@app.post("/chat/{room_id}/members/{member_id}/role")
async def chat_set_member_role(
    request: Request,
    room_id: int,
    member_id: int,
    role: str = Form(...),
    db: Session = Depends(get_db),
):
    cm = _current_member(request, db)
    actor = _chat_member(db, room_id, cm.id) if cm else None
    if not actor or actor.role != "owner":
        raise HTTPException(status_code=403)
    target = _chat_member(db, room_id, member_id)
    room = db.query(ChatRoom).filter(ChatRoom.id == room_id).first()
    if not target or not room:
        raise HTTPException(status_code=404)
    if role == "transfer_owner":
        actor.role = "member"
        target.role = "owner"
        room.created_by_id = target.member_id
    elif role in ("co_owner", "member") and target.role != "owner":
        target.role = role
    db.commit()
    return RedirectResponse(url=f"/chat/{room_id}", status_code=303)


@app.post("/chat/{room_id}/members/{member_id}/mute")
async def chat_mute_member(
    request: Request,
    room_id: int,
    member_id: int,
    duration_minutes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    cm = _current_member(request, db)
    actor = _chat_member(db, room_id, cm.id) if cm else None
    if not _is_privileged(request, db) and not (actor and actor.role in ("owner", "co_owner")):
        raise HTTPException(status_code=403)
    target = _chat_member(db, room_id, member_id)
    if not target:
        raise HTTPException(status_code=404)
    if target.role == "owner" and not _is_privileged(request, db):
        raise HTTPException(status_code=403, detail="방장은 채팅 제한할 수 없습니다.")
    minutes = _optional_int(duration_minutes, "채팅 금지 시간")
    target.muted_until = (datetime.now() + timedelta(minutes=minutes)) if minutes and minutes > 0 else None
    db.commit()
    return RedirectResponse(url=f"/chat/{room_id}", status_code=303)


@app.get("/chat/{room_id}", response_class=HTMLResponse)
async def chat_room(request: Request, room_id: int, db: Session = Depends(get_db)):
    cm = _current_member(request, db)
    if not cm:
        return RedirectResponse(url="/member/login", status_code=303)

    room = db.query(ChatRoom).filter(ChatRoom.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404)
    room_member = _chat_member(db, room.id, cm.id)
    if not room.password_hash:
        room_member = _ensure_chat_member(db, room, cm)

    # 최근 메시지 100개
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.room_id == room_id)
        .order_by(ChatMessage.created_at.asc())
        .limit(100)
        .all()
    )
    author_ids = list({m.author_id for m in messages})
    authors = _member_map(db, author_ids)
    for msg in messages:
        msg.author = authors.get(msg.author_id)

    resp = _render(request,
        "chat/room.html",
        _ctx(
            request,
            db,
            room=room,
            messages=messages,
            has_password=bool(room.password_hash and not room_member),
            room_member=room_member,
            room_members=_room_members(db, room.id),
            online_members=_room_mgr.online(room.id),
            can_manage_room=_can_manage_room(room_member, request, db),
        ),
    )
    # ws_token: non-httpOnly, JS에서 읽어 WebSocket URL에 사용
    ws_tok = create_member_token(cm.id)
    resp.set_cookie("ws_token", ws_tok, httponly=False, max_age=3600, samesite="lax")
    return resp


@app.websocket("/ws/chat/{room_id}")
async def ws_chat(
    ws: WebSocket, room_id: int,
    token: str = Query(""),
    password: str = Query(""),
):
    mid = verify_member_token(token)
    if not mid:
        await ws.close(code=4001, reason="Unauthorized")
        return

    db = SessionLocal()
    try:
        room = db.query(ChatRoom).filter(ChatRoom.id == room_id).first()
        if not room:
            await ws.close(code=4004, reason="Room not found")
            return

        member = db.query(Member).filter(Member.id == mid).first()
        if not member:
            await ws.close(code=4001, reason="Member not found")
            return
        room_member = _chat_member(db, room_id, member.id)
        if room.password_hash and not room_member:
            if not password or not _verify_pw(password, room.password_hash):
                await ws.close(code=4003, reason="Wrong password")
                return
        room_member = _ensure_chat_member(db, room, member)

        await _room_mgr.join(room_id, ws, member)
        await _broadcast_room_state(room_id, db)
        await _room_mgr.broadcast(room_id, {
            "type": "system",
            "message": f"{member.activity_name}님이 입장했습니다.",
        })

        try:
            while True:
                text = await ws.receive_text()
                content = text.strip()[:2000]
                if not content:
                    continue
                db.refresh(room_member)
                if room_member.muted_until and room_member.muted_until > datetime.now():
                    await ws.send_json({
                        "type": "error",
                        "message": f"{room_member.muted_until.strftime('%Y.%m.%d %H:%M')}까지 채팅이 제한되었습니다.",
                    })
                    continue
                msg = ChatMessage(room_id=room_id, author_id=mid, content=content)
                db.add(msg)
                db.commit()
                db.refresh(msg)
                await _room_mgr.broadcast(room_id, {
                    "type": "message",
                    "id": msg.id,
                    "author": member.activity_name,
                    "profile_image": member.profile_image,
                    "content": content,
                    "time": msg.created_at.strftime("%H:%M"),
                })
        except WebSocketDisconnect:
            pass
        finally:
            _room_mgr.leave(room_id, ws)
            await _broadcast_room_state(room_id, db)
            await _room_mgr.broadcast(room_id, {
                "type": "system",
                "message": f"{member.activity_name}님이 퇴장했습니다.",
            })
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════════════════
#  관리자 — 공모전 자동 크롤링 (Phase 3)
# ════════════════════════════════════════════════════════════════════════════

# 크롤 결과를 서버 인스턴스 메모리에 캐시 (재크롤 전까지 유지)
_crawl_cache: dict = {"items": [], "errors": [], "counts": {}, "crawled_at": None}


@app.get("/admin/crawl", response_class=HTMLResponse)
async def admin_crawl_page(request: Request, db: Session = Depends(get_db)):
    if r := _admin_redirect(request):
        return r
    return _render(request,
        "admin/crawl.html",
        _ctx(request, db, cache=_crawl_cache),
    )


@app.post("/admin/crawl/run")
async def admin_crawl_run(request: Request, db: Session = Depends(get_db)):
    """크롤링 실행 — 완료까지 기다린 후 결과 페이지로 이동"""
    if r := _admin_redirect(request):
        return r
    global _crawl_cache
    result = await _do_crawl_all()
    result["crawled_at"] = datetime.now().strftime("%Y년 %m월 %d일 %H:%M")
    _crawl_cache = result
    return RedirectResponse(url="/admin/crawl", status_code=303)


@app.post("/admin/crawl/add")
async def admin_crawl_add(
    request: Request,
    idx: int = Form(...),
    db: Session = Depends(get_db),
):
    """크롤 결과 한 항목을 공모전으로 즉시 등록 (기본 정보만)"""
    if r := _admin_redirect(request):
        return r

    items = _crawl_cache.get("items", [])
    if idx < 0 or idx >= len(items):
        raise HTTPException(status_code=400, detail="잘못된 인덱스입니다.")

    item = items[idx]
    deadline_str = item.get("deadline")
    if not deadline_str:
        # 마감일 없는 경우 오늘+30일로 임시 설정
        deadline_str = (date.today() + timedelta(days=30)).isoformat()

    comp = Competition(
        title=item.get("title", ""),
        organizer=item.get("organizer", ""),
        tags=json.dumps(item.get("tags", []), ensure_ascii=False),
        deadline=date.fromisoformat(deadline_str),
        prize=item.get("prize", ""),
        link=item.get("link", ""),
        description=f"[{item.get('source_label', '')}에서 자동 수집]\n\n원문 링크: {item.get('link', '')}",
    )
    db.add(comp)
    db.commit()
    return RedirectResponse(url=f"/admin/edit/{comp.id}", status_code=303)


@app.post("/admin/crawl/add-with-gpt")
async def admin_crawl_add_with_gpt(
    request: Request,
    idx: int = Form(...),
    db: Session = Depends(get_db),
):
    """크롤 결과의 링크를 GPT로 파싱한 뒤 추가 (공고 본문 자동 추출)"""
    if r := _admin_redirect(request):
        return r

    items = _crawl_cache.get("items", [])
    if idx < 0 or idx >= len(items):
        raise HTTPException(status_code=400, detail="잘못된 인덱스입니다.")

    item = items[idx]
    link = item.get("link", "")

    try:
        async with __import__("httpx").AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(link, headers={"User-Agent": "Mozilla/5.0"})
        from bs4 import BeautifulSoup as _BS
        soup = _BS(r.text, "lxml")
        # 본문 텍스트 추출 (script/style 제거 후 앞 6000자)
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        page_text = re.sub(r"\s{3,}", "\n\n", soup.get_text()).strip()[:6000]
        parsed = await parse_text(page_text)
    except Exception as exc:
        # GPT 파싱 실패 시 기본 등록으로 폴백
        parsed = {
            "title": item.get("title", ""),
            "organizer": item.get("organizer", ""),
            "deadline": item.get("deadline"),
            "link": link,
            "description": f"GPT 파싱 실패: {exc}\n\n원문: {link}",
        }

    deadline_str = parsed.get("deadline") or item.get("deadline")
    if not deadline_str:
        deadline_str = (date.today() + timedelta(days=30)).isoformat()

    comp = Competition(
        title=parsed.get("title") or item.get("title", ""),
        organizer=parsed.get("organizer") or item.get("organizer", ""),
        tags=json.dumps(parsed.get("tags") or item.get("tags", []), ensure_ascii=False),
        start_date=date.fromisoformat(parsed["start_date"]) if parsed.get("start_date") else None,
        deadline=date.fromisoformat(deadline_str),
        announcement_date=date.fromisoformat(parsed["announcement_date"]) if parsed.get("announcement_date") else None,
        prize=parsed.get("prize") or item.get("prize", ""),
        link=link,
        description=parsed.get("description", ""),
    )
    db.add(comp)
    db.commit()
    return RedirectResponse(url=f"/admin/edit/{comp.id}", status_code=303)
