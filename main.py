import hashlib
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
from auth import create_token, verify_token
from database import SessionLocal, get_db, init_db
from member_auth import create_member_token, hash_password, verify_member_token, verify_password
from models import (
    BOARDS,
    ChatMessage, ChatRoom,
    Comment, CommentLike,
    Competition, InviteCode, Member,
    Post, PostLike,
    TeamMember,
)

app = FastAPI(title="공모전 보드")

UPLOAD_DIR = BASE_DIR / os.getenv("UPLOAD_DIR", "uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _from_json(value) -> list:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


templates.env.filters["fromjson"] = _from_json

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin1234")
TAGS = ["IT/SW", "디자인", "기획·마케팅", "사회혁신", "예술·문화", "창업·스타트업", "논문·학술", "기타"]
ROLES = ["기획", "개발", "디자인", "마케팅", "기타"]


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
    }
    base.update(extra)
    return base


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
        query = query.filter(or_(Competition.title.contains(q), Competition.organizer.contains(q)))

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

    return templates.TemplateResponse(
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

    members = (
        db.query(TeamMember)
        .filter(TeamMember.competition_id == comp_id)
        .order_by(TeamMember.created_at.asc())
        .all()
    )

    today = date.today()
    submission_window = comp.deadline < today <= comp.deadline + timedelta(days=7)

    return templates.TemplateResponse(
        "detail.html",
        _ctx(request, db,
             comp=comp, files=_from_json(comp.files),
             tags_list=_from_json(comp.tags),
             members=members, roles=ROLES,
             submission_window=submission_window, today=today),
    )


# ════════════════════════════════════════════════════════════════════════════
#  관리자 — 인증 / 대시보드
# ════════════════════════════════════════════════════════════════════════════

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request, db: Session = Depends(get_db)):
    if _is_admin(request):
        return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse("admin/login.html", _ctx(request, db, error=None))


@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...), db: Session = Depends(get_db)):
    if password == ADMIN_PASSWORD:
        resp = RedirectResponse(url="/admin", status_code=303)
        resp.set_cookie("admin_token", create_token(), httponly=True, max_age=86400, samesite="lax")
        return resp
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
        "admin/dashboard.html",
        _ctx(request, db, competitions=competitions, today=date.today()),
    )


# ── 공모전 CRUD ───────────────────────────────────────────────────────────────

@app.get("/admin/add", response_class=HTMLResponse)
async def admin_add_page(request: Request, db: Session = Depends(get_db)):
    if r := _admin_redirect(request):
        return r
    return templates.TemplateResponse(
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
    max_members: Optional[int] = Form(None),
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
        image=image, max_members=max_members, is_featured=is_featured,
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
    return templates.TemplateResponse(
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
    is_featured: bool = Form(False), max_members: Optional[int] = Form(None),
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
    comp.is_featured = is_featured; comp.max_members = max_members
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
    return templates.TemplateResponse("admin/members.html", _ctx(request, db, members=members))


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


# ── 초대 코드 ─────────────────────────────────────────────────────────────────

@app.get("/admin/invite-codes", response_class=HTMLResponse)
async def admin_invite_codes(request: Request, db: Session = Depends(get_db)):
    if r := _admin_redirect(request):
        return r
    codes = db.query(InviteCode).order_by(InviteCode.created_at.desc()).all()
    used_ids = [c.used_by_member_id for c in codes if c.used_by_member_id]
    members_map = {}
    if used_ids:
        for m in db.query(Member).filter(Member.id.in_(used_ids)).all():
            members_map[m.id] = m.activity_name
    return templates.TemplateResponse(
        "admin/invite_codes.html",
        _ctx(request, db, codes=codes, members_map=members_map, now=datetime.now()),
    )


@app.post("/admin/invite-codes/create")
async def admin_create_invite_code(request: Request, note: str = Form(""), db: Session = Depends(get_db)):
    if r := _admin_redirect(request):
        return r
    db.add(InviteCode(code=secrets.token_urlsafe(12), note=note))
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


# ════════════════════════════════════════════════════════════════════════════
#  회원 — 가입 / 로그인 / 로그아웃 / 프로필
# ════════════════════════════════════════════════════════════════════════════

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, db: Session = Depends(get_db)):
    if _current_member(request, db):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("register.html", _ctx(request, db, error=None))


@app.post("/register")
async def register(
    request: Request,
    invite_code: str = Form(...), activity_name: str = Form(...), real_name: str = Form(...),
    student_id: str = Form(""), phone: str = Form(""),
    password: str = Form(...), bio: str = Form(""),
    db: Session = Depends(get_db),
):
    def err(msg):
        return templates.TemplateResponse("register.html", _ctx(request, db, error=msg), status_code=400)

    code_obj = db.query(InviteCode).filter(
        InviteCode.code == invite_code.strip(),
        InviteCode.used_by_member_id.is_(None),
    ).first()
    if not code_obj:
        return err("초대 코드가 올바르지 않거나 이미 사용된 코드입니다.")
    if code_obj.expires_at and datetime.now() > code_obj.expires_at:
        return err("만료된 초대 코드입니다.")
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
    code_obj.used_by_member_id = member.id
    db.commit()

    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie("member_token", create_member_token(member.id), httponly=True, max_age=604800, samesite="lax")
    return resp


@app.get("/member/login", response_class=HTMLResponse)
async def member_login_page(request: Request, db: Session = Depends(get_db)):
    if _current_member(request, db):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("member_login.html", _ctx(request, db, error=None))


@app.post("/member/login")
async def member_login(request: Request, activity_name: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    m = db.query(Member).filter(Member.activity_name == activity_name.strip()).first()
    if not m or not verify_password(password, m.password_hash):
        return templates.TemplateResponse(
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
    return templates.TemplateResponse("profile.html", _ctx(request, db, target=target, is_own=bool(cm and cm.id == target.id)))


@app.get("/profile/edit/me", response_class=HTMLResponse)
async def profile_edit_page(request: Request, db: Session = Depends(get_db)):
    cm = _current_member(request, db)
    if not cm:
        return RedirectResponse(url="/member/login", status_code=303)
    return templates.TemplateResponse("profile_edit.html", _ctx(request, db, member=cm, error=None))


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
        return templates.TemplateResponse("profile_edit.html", _ctx(request, db, member=cm, error="현재 비밀번호가 올바르지 않습니다."), status_code=400)

    cm.bio = bio.strip(); cm.real_name = real_name.strip(); cm.phone = phone.strip()
    new_img = await _save_image(profile_image)
    if new_img:
        cm.profile_image = new_img
    if new_password:
        if len(new_password) < 6:
            return templates.TemplateResponse("profile_edit.html", _ctx(request, db, member=cm, error="새 비밀번호는 최소 6자 이상이어야 합니다."), status_code=400)
        cm.password_hash = hash_password(new_password)
    db.commit()
    return RedirectResponse(url=f"/profile/{cm.activity_name}", status_code=303)


# ════════════════════════════════════════════════════════════════════════════
#  팀 구성
# ════════════════════════════════════════════════════════════════════════════

@app.post("/competition/{comp_id}/join")
async def team_join(
    request: Request, comp_id: int,
    nickname: str = Form(...), password: str = Form(...),
    role: str = Form("기타"), memo: str = Form(""),
    db: Session = Depends(get_db),
):
    comp = db.query(Competition).filter(Competition.id == comp_id).first()
    if not comp:
        raise HTTPException(status_code=404)
    members = db.query(TeamMember).filter(TeamMember.competition_id == comp_id).all()
    if comp.max_members and len(members) >= comp.max_members:
        raise HTTPException(status_code=400, detail="팀 인원이 가득 찼습니다.")
    if db.query(TeamMember).filter(TeamMember.competition_id == comp_id, TeamMember.nickname == nickname).first():
        raise HTTPException(status_code=400, detail="이미 같은 닉네임으로 참여 중입니다.")
    cm = _current_member(request, db)
    db.add(TeamMember(
        competition_id=comp_id, nickname=nickname,
        password_hash=_hash_pw(password),
        role=role if role in ROLES else "기타",
        memo=memo, is_leader=len(members) == 0,
        member_id=cm.id if cm else None,
    ))
    db.commit()
    return RedirectResponse(url=f"/competition/{comp_id}#team", status_code=303)


@app.post("/competition/{comp_id}/leave/{member_id}")
async def team_leave(
    request: Request, comp_id: int, member_id: int,
    nickname: str = Form(...), password: str = Form(...),
    db: Session = Depends(get_db),
):
    member = db.query(TeamMember).filter(TeamMember.id == member_id, TeamMember.competition_id == comp_id).first()
    if not member:
        raise HTTPException(status_code=404)
    if not _is_privileged(request, db):
        if member.nickname != nickname or not _verify_pw(password, member.password_hash):
            raise HTTPException(status_code=400, detail="닉네임 또는 비밀번호가 올바르지 않습니다.")
    was_leader = member.is_leader
    db.delete(member)
    db.commit()
    if was_leader:
        next_leader = db.query(TeamMember).filter(TeamMember.competition_id == comp_id).order_by(TeamMember.created_at.asc()).first()
        if next_leader:
            next_leader.is_leader = True
            db.commit()
    return RedirectResponse(url=f"/competition/{comp_id}#team", status_code=303)


@app.post("/admin/competition/{comp_id}/set-leader/{member_id}")
async def set_leader(request: Request, comp_id: int, member_id: int, db: Session = Depends(get_db)):
    if not _is_privileged(request, db):
        raise HTTPException(status_code=403)
    db.query(TeamMember).filter(TeamMember.competition_id == comp_id, TeamMember.is_leader.is_(True)).update({"is_leader": False})
    m = db.query(TeamMember).filter(TeamMember.id == member_id).first()
    if m:
        m.is_leader = True
    db.commit()
    return RedirectResponse(url=f"/competition/{comp_id}#team", status_code=303)


@app.post("/competition/{comp_id}/submit")
async def record_submission(
    request: Request, comp_id: int,
    participant_ids: List[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    if not _is_privileged(request, db):
        raise HTTPException(status_code=403)
    comp = db.query(Competition).filter(Competition.id == comp_id).first()
    if not comp:
        raise HTTPException(status_code=404)
    today = date.today()
    if not (comp.deadline < today <= comp.deadline + timedelta(days=7)):
        raise HTTPException(status_code=400, detail="제출 기록 기간이 아닙니다.")
    db.query(TeamMember).filter(TeamMember.competition_id == comp_id).update({"is_participant": False})
    if participant_ids:
        db.query(TeamMember).filter(TeamMember.competition_id == comp_id, TeamMember.id.in_(participant_ids)).update({"is_participant": True})
    comp.submitted = True
    comp.submitted_at = datetime.now()
    db.commit()
    return RedirectResponse(url=f"/competition/{comp_id}#team", status_code=303)


# ════════════════════════════════════════════════════════════════════════════
#  게시판
# ════════════════════════════════════════════════════════════════════════════

@app.get("/board/{board}", response_class=HTMLResponse)
async def board_list(
    request: Request, board: str,
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
):
    if board not in BOARDS:
        raise HTTPException(status_code=404)

    page_size = 20
    total = db.query(func.count(Post.id)).filter(Post.board == board).scalar()
    posts = (
        db.query(Post)
        .filter(Post.board == board)
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

    return templates.TemplateResponse(
        "board/list.html",
        _ctx(request, db,
             board=board, board_name=BOARDS[board],
             posts=posts, page=page, total_pages=total_pages),
    )


@app.get("/board/{board}/new", response_class=HTMLResponse)
async def board_new_page(request: Request, board: str, db: Session = Depends(get_db)):
    if board not in BOARDS:
        raise HTTPException(status_code=404)
    cm = _current_member(request, db)
    if not cm:
        return RedirectResponse(url="/member/login", status_code=303)
    return templates.TemplateResponse(
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

    return templates.TemplateResponse(
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
        self.connections: dict[int, set] = defaultdict(set)

    async def join(self, room_id: int, ws: WebSocket):
        await ws.accept()
        self.connections[room_id].add(ws)

    def leave(self, room_id: int, ws: WebSocket):
        self.connections[room_id].discard(ws)

    async def broadcast(self, room_id: int, msg: dict):
        dead = set()
        for ws in list(self.connections.get(room_id, set())):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self.connections[room_id] -= dead


_room_mgr = _RoomManager()


@app.get("/chat", response_class=HTMLResponse)
async def chat_list(request: Request, db: Session = Depends(get_db)):
    cm = _current_member(request, db)
    if not cm:
        return RedirectResponse(url="/member/login", status_code=303)

    rooms = db.query(ChatRoom).order_by(ChatRoom.created_at.desc()).all()
    creator_ids = list({r.created_by_id for r in rooms})
    creators = _member_map(db, creator_ids)
    for r in rooms:
        r.creator = creators.get(r.created_by_id)
        r.online_count = len(_room_mgr.connections.get(r.id, set()))
        r.has_password = bool(r.password_hash)

    return templates.TemplateResponse("chat/list.html", _ctx(request, db, rooms=rooms))


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


@app.get("/chat/{room_id}", response_class=HTMLResponse)
async def chat_room(request: Request, room_id: int, db: Session = Depends(get_db)):
    cm = _current_member(request, db)
    if not cm:
        return RedirectResponse(url="/member/login", status_code=303)

    room = db.query(ChatRoom).filter(ChatRoom.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404)

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

    resp = templates.TemplateResponse(
        "chat/room.html",
        _ctx(request, db, room=room, messages=messages, has_password=bool(room.password_hash)),
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

        if room.password_hash and not _verify_pw(password, room.password_hash):
            await ws.close(code=4003, reason="Wrong password")
            return

        member = db.query(Member).filter(Member.id == mid).first()
        if not member:
            await ws.close(code=4001, reason="Member not found")
            return

        await _room_mgr.join(room_id, ws)
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
            await _room_mgr.broadcast(room_id, {
                "type": "system",
                "message": f"{member.activity_name}님이 퇴장했습니다.",
            })
    finally:
        db.close()
