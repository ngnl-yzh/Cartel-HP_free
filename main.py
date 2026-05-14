import hashlib
import json
import os
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
from sqlalchemy import case, or_
from sqlalchemy.orm import Session

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

from ai_parser import parse_image_file, parse_text
from auth import create_token, verify_token
from database import get_db, init_db
from models import Competition, TeamMember

app = FastAPI(title="공모전 보드")

UPLOAD_DIR = BASE_DIR / os.getenv("UPLOAD_DIR", "uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _from_json(value: str | None) -> list:
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


@app.on_event("startup")
def startup():
    init_db()


def _days_left(deadline: date) -> int:
    return (deadline - date.today()).days


def _urgency(deadline: date) -> str:
    days_left = _days_left(deadline)
    if days_left < 0:
        return "closed"
    if days_left <= 7:
        return "urgent"
    if days_left <= 30:
        return "soon"
    return "open"


def _annotate(competitions: list[Competition]) -> list[Competition]:
    for competition in competitions:
        competition.status = _urgency(competition.deadline)
        competition.days_left = _days_left(competition.deadline)
    return competitions


def _is_admin(request: Request) -> bool:
    token = request.cookies.get("admin_token")
    return bool(token and verify_token(token))


def _admin_redirect(request: Request):
    if not _is_admin(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    return None


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

    # 카드에 팀 인원 수 표시용
    all_ids = [c.id for c in competitions] + [c.id for c in featured]
    from sqlalchemy import func
    counts = dict(
        db.query(TeamMember.competition_id, func.count(TeamMember.id))
        .filter(TeamMember.competition_id.in_(all_ids))
        .group_by(TeamMember.competition_id)
        .all()
    ) if all_ids else {}
    for c in competitions + featured:
        c.member_count = counts.get(c.id, 0)

    return templates.TemplateResponse(
        name="index.html",
        request=request,
        context={
            "request": request,
            "featured": featured,
            "competitions": competitions,
            "tags": TAGS,
            "current_tag": tag or "all",
            "current_sort": sort,
            "query": q,
            "today": today,
            "is_admin": _is_admin(request),
        },
    )


@app.get("/competition/{comp_id}", response_class=HTMLResponse)
async def detail(request: Request, comp_id: int, db: Session = Depends(get_db)):
    competition = db.query(Competition).filter(Competition.id == comp_id).first()
    if not competition:
        raise HTTPException(status_code=404, detail="공모전을 찾을 수 없습니다.")

    competition.view_count += 1
    db.commit()
    db.refresh(competition)

    competition.status = _urgency(competition.deadline)
    competition.days_left = _days_left(competition.deadline)

    members = db.query(TeamMember).filter(
        TeamMember.competition_id == comp_id
    ).order_by(TeamMember.created_at.asc()).all()

    return templates.TemplateResponse(
        name="detail.html",
        request=request,
        context={
            "request": request,
            "comp": competition,
            "files": _from_json(competition.files),
            "tags_list": _from_json(competition.tags),
            "members": members,
            "roles": ROLES,
            "is_admin": _is_admin(request),
        },
    )


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    if _is_admin(request):
        return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse(
        name="admin/login.html",
        request=request,
        context={"request": request, "error": None},
    )


@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...)):
    if password == ADMIN_PASSWORD:
        response = RedirectResponse(url="/admin", status_code=303)
        response.set_cookie("admin_token", create_token(), httponly=True, max_age=86400, samesite="lax")
        return response
    return templates.TemplateResponse(
        name="admin/login.html",
        request=request,
        context={"request": request, "error": "비밀번호가 올바르지 않습니다."},
        status_code=401,
    )


@app.get("/admin/logout")
async def admin_logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("admin_token")
    return response


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    redirect = _admin_redirect(request)
    if redirect:
        return redirect

    competitions = _annotate(db.query(Competition).order_by(Competition.deadline.asc()).all())
    return templates.TemplateResponse(
        name="admin/dashboard.html",
        request=request,
        context={"request": request, "competitions": competitions, "today": date.today(), "is_admin": True},
    )


@app.get("/admin/add", response_class=HTMLResponse)
async def admin_add_page(request: Request):
    redirect = _admin_redirect(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        name="admin/form.html",
        request=request,
        context={
            "request": request,
            "comp": None,
            "tags": TAGS,
            "action": "/admin/add",
            "title": "공모전 추가",
            "is_admin": True,
        },
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
    comp_image_path: Optional[str] = Form(None),   # GPT 파싱 시 자동 저장된 경로
    comp_image: Optional[UploadFile] = File(None),  # 직접 업로드한 이미지
    files: List[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    redirect = _admin_redirect(request)
    if redirect:
        return redirect

    image = await _save_image(comp_image) or comp_image_path or None

    competition = Competition(
        title=title,
        organizer=organizer,
        tags=json.dumps(tags, ensure_ascii=False),
        start_date=date.fromisoformat(start_date) if start_date else None,
        deadline=date.fromisoformat(deadline),
        announcement_date=date.fromisoformat(announcement_date) if announcement_date else None,
        prize=prize,
        link=link,
        description=description,
        image=image,
        max_members=max_members,
        is_featured=is_featured,
        files=json.dumps(await _save_files(files), ensure_ascii=False),
    )
    db.add(competition)
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/admin/edit/{comp_id}", response_class=HTMLResponse)
async def admin_edit_page(request: Request, comp_id: int, db: Session = Depends(get_db)):
    redirect = _admin_redirect(request)
    if redirect:
        return redirect

    competition = db.query(Competition).filter(Competition.id == comp_id).first()
    if not competition:
        raise HTTPException(status_code=404)

    competition.tags_list = _from_json(competition.tags)
    competition.files_list = _from_json(competition.files)
    return templates.TemplateResponse(
        name="admin/form.html",
        request=request,
        context={
            "request": request,
            "comp": competition,
            "tags": TAGS,
            "action": f"/admin/edit/{comp_id}",
            "title": "공모전 수정",
            "is_admin": True,
        },
    )


@app.post("/admin/edit/{comp_id}")
async def admin_edit(
    request: Request,
    comp_id: int,
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
    redirect = _admin_redirect(request)
    if redirect:
        return redirect

    competition = db.query(Competition).filter(Competition.id == comp_id).first()
    if not competition:
        raise HTTPException(status_code=404)

    new_image = await _save_image(comp_image)
    existing_files = _from_json(competition.files)
    competition.title = title
    competition.organizer = organizer
    competition.tags = json.dumps(tags, ensure_ascii=False)
    competition.start_date = date.fromisoformat(start_date) if start_date else None
    competition.deadline = date.fromisoformat(deadline)
    competition.announcement_date = date.fromisoformat(announcement_date) if announcement_date else None
    competition.prize = prize
    competition.link = link
    competition.description = description
    competition.is_featured = is_featured
    competition.max_members = max_members
    competition.image = new_image or comp_image_path or competition.image
    competition.files = json.dumps(existing_files + await _save_files(files), ensure_ascii=False)
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete/{comp_id}")
async def admin_delete(request: Request, comp_id: int, db: Session = Depends(get_db)):
    redirect = _admin_redirect(request)
    if redirect:
        return redirect

    competition = db.query(Competition).filter(Competition.id == comp_id).first()
    if competition:
        for item in _from_json(competition.files):
            try:
                (UPLOAD_DIR / item["path"]).unlink(missing_ok=True)
            except OSError:
                pass
        db.delete(competition)
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete-file/{comp_id}")
async def delete_file(request: Request, comp_id: int, filename: str = Form(...), db: Session = Depends(get_db)):
    redirect = _admin_redirect(request)
    if redirect:
        return redirect

    competition = db.query(Competition).filter(Competition.id == comp_id).first()
    if competition:
        files = [item for item in _from_json(competition.files) if item.get("path") != filename]
        competition.files = json.dumps(files, ensure_ascii=False)
        db.commit()
        try:
            (UPLOAD_DIR / filename).unlink(missing_ok=True)
        except OSError:
            pass
    return RedirectResponse(url=f"/admin/edit/{comp_id}", status_code=303)


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
        # 이미지를 파싱하면서 동시에 uploads/에 저장
        ext = Path(image.filename).suffix.lower() if image.filename else ".jpg"
        stored_name = f"{uuid.uuid4().hex}{ext}"
        (UPLOAD_DIR / stored_name).write_bytes(data)

        result = await parse_image_file(data, image.content_type)
        result["_image_path"] = stored_name   # 폼에서 대표 이미지로 사용
        return JSONResponse(result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── 비밀번호 헬퍼 ──────────────────────────────────────────────────────────────

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


# ── 팀 참여 / 취소 / 팀장 지정 ────────────────────────────────────────────────

ROLES = ["기획", "개발", "디자인", "마케팅", "기타"]


@app.post("/competition/{comp_id}/join")
async def team_join(
    request: Request,
    comp_id: int,
    nickname: str = Form(...),
    password: str = Form(...),
    role: str = Form("기타"),
    memo: str = Form(""),
    db: Session = Depends(get_db),
):
    comp = db.query(Competition).filter(Competition.id == comp_id).first()
    if not comp:
        raise HTTPException(status_code=404)

    members = db.query(TeamMember).filter(TeamMember.competition_id == comp_id).all()

    # 인원 초과 확인
    if comp.max_members and len(members) >= comp.max_members:
        raise HTTPException(status_code=400, detail="팀 인원이 가득 찼습니다.")

    # 닉네임 중복 확인
    dup = db.query(TeamMember).filter(
        TeamMember.competition_id == comp_id,
        TeamMember.nickname == nickname,
    ).first()
    if dup:
        raise HTTPException(status_code=400, detail="이미 같은 닉네임으로 참여 중입니다.")

    member = TeamMember(
        competition_id=comp_id,
        nickname=nickname,
        password_hash=_hash_pw(password),
        role=role if role in ROLES else "기타",
        memo=memo,
        is_leader=len(members) == 0,   # 첫 번째 참여자는 자동 팀장
    )
    db.add(member)
    db.commit()
    return RedirectResponse(url=f"/competition/{comp_id}#team", status_code=303)


@app.post("/competition/{comp_id}/leave/{member_id}")
async def team_leave(
    request: Request,
    comp_id: int,
    member_id: int,
    nickname: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    member = db.query(TeamMember).filter(
        TeamMember.id == member_id,
        TeamMember.competition_id == comp_id,
    ).first()
    if not member:
        raise HTTPException(status_code=404)

    # 관리자는 비밀번호 없이 삭제 가능
    if not _is_admin(request):
        if member.nickname != nickname or not _verify_pw(password, member.password_hash):
            raise HTTPException(status_code=400, detail="닉네임 또는 비밀번호가 올바르지 않습니다.")

    was_leader = member.is_leader
    db.delete(member)
    db.commit()

    # 팀장이 나갔으면 가장 오래된 멤버를 팀장으로 승격
    if was_leader:
        next_leader = db.query(TeamMember).filter(
            TeamMember.competition_id == comp_id
        ).order_by(TeamMember.created_at.asc()).first()
        if next_leader:
            next_leader.is_leader = True
            db.commit()

    return RedirectResponse(url=f"/competition/{comp_id}#team", status_code=303)


@app.post("/admin/competition/{comp_id}/set-leader/{member_id}")
async def set_leader(
    request: Request,
    comp_id: int,
    member_id: int,
    db: Session = Depends(get_db),
):
    if not _is_admin(request):
        raise HTTPException(status_code=403)

    # 기존 팀장 해제
    db.query(TeamMember).filter(
        TeamMember.competition_id == comp_id,
        TeamMember.is_leader == True,
    ).update({"is_leader": False})

    # 새 팀장 지정
    member = db.query(TeamMember).filter(TeamMember.id == member_id).first()
    if member:
        member.is_leader = True
    db.commit()
    return RedirectResponse(url=f"/competition/{comp_id}#team", status_code=303)


async def _save_image(upload: Optional[UploadFile]) -> Optional[str]:
    """대표 이미지 1장을 저장하고 파일명을 반환. 없으면 None."""
    if not upload or not upload.filename:
        return None
    content = await upload.read()
    if not content:
        return None
    ext = Path(upload.filename).suffix.lower()
    stored_name = f"{uuid.uuid4().hex}{ext}"
    (UPLOAD_DIR / stored_name).write_bytes(content)
    return stored_name


async def _save_files(files: List[UploadFile]) -> list[dict[str, str]]:
    saved_files = []
    for upload in files or []:
        if not upload.filename:
            continue
        content = await upload.read()
        if not content:
            continue
        extension = Path(upload.filename).suffix.lower()
        stored_name = f"{uuid.uuid4().hex}{extension}"
        (UPLOAD_DIR / stored_name).write_bytes(content)
        saved_files.append({"name": upload.filename, "path": stored_name})
    return saved_files
