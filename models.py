from datetime import datetime

from sqlalchemy import Boolean, Column, Date, DateTime, Integer, String, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()

BOARDS = {
    "free":    "자유게시판",
    "social":  "활동(친목)",
    "project": "활동(프로젝트)",
}


class Competition(Base):
    __tablename__ = "competitions"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(300), nullable=False)
    organizer = Column(String(200), default="")
    tags = Column(String(500), default="[]")
    start_date = Column(Date, nullable=True)
    deadline = Column(Date, nullable=False)
    announcement_date = Column(Date, nullable=True)
    prize = Column(String(500), default="")
    link = Column(String(1000), default="")
    description = Column(Text, default="")
    files = Column(Text, default="[]")
    view_count = Column(Integer, default=0)
    image = Column(String(500), nullable=True)
    max_members = Column(Integer, nullable=True)
    is_featured = Column(Boolean, default=False)
    submitted = Column(Boolean, default=False)
    submitted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class TeamMember(Base):
    __tablename__ = "team_members"

    id = Column(Integer, primary_key=True, index=True)
    competition_id = Column(Integer, nullable=False, index=True)
    nickname = Column(String(100), nullable=False)
    password_hash = Column(String(300), nullable=False)
    role = Column(String(50), default="기타")
    memo = Column(String(500), default="")
    is_leader = Column(Boolean, default=False)
    is_participant = Column(Boolean, default=False)
    member_id = Column(Integer, nullable=True)
    # ── 수상 정보 ──────────────────────────────────────────────────
    award_rank = Column(String(50), nullable=True)     # 대상/최우수상/우수상/장려상/입선
    award_prize = Column(String(300), default="")      # 상금·부상 내용
    award_note = Column(Text, default="")              # 수상 관련 메모
    created_at = Column(DateTime, default=datetime.now)


class Member(Base):
    __tablename__ = "members"

    id = Column(Integer, primary_key=True, index=True)
    activity_name = Column(String(100), unique=True, nullable=False, index=True)
    real_name = Column(String(100), nullable=False)
    student_id = Column(String(50), default="")
    phone = Column(String(50), default="")
    password_hash = Column(String(300), nullable=False)
    bio = Column(Text, default="")
    profile_image = Column(String(500), nullable=True)
    role = Column(String(20), default="member")   # member / sub_admin
    invite_code_used = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class InviteCode(Base):
    __tablename__ = "invite_codes"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(100), unique=True, nullable=False, index=True)
    note = Column(String(200), default="")
    created_at = Column(DateTime, default=datetime.now)
    expires_at = Column(DateTime, nullable=True)
    used_by_member_id = Column(Integer, nullable=True)


# ── 게시판 ────────────────────────────────────────────────────────────────────

class Post(Base):
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, index=True)
    board = Column(String(20), nullable=False, index=True)   # free / social / project
    title = Column(String(300), nullable=False)
    content = Column(Text, default="")
    author_id = Column(Integer, nullable=False, index=True)
    images = Column(Text, default="[]")              # JSON list of filenames
    view_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now)


class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, nullable=False, index=True)
    parent_id = Column(Integer, nullable=True)       # None = 최상위 댓글
    author_id = Column(Integer, nullable=False, index=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.now)


class PostLike(Base):
    __tablename__ = "post_likes"

    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, nullable=False, index=True)
    member_id = Column(Integer, nullable=False, index=True)


class CommentLike(Base):
    __tablename__ = "comment_likes"

    id = Column(Integer, primary_key=True, index=True)
    comment_id = Column(Integer, nullable=False, index=True)
    member_id = Column(Integer, nullable=False, index=True)


# ── 채팅 ──────────────────────────────────────────────────────────────────────

class ChatRoom(Base):
    __tablename__ = "chat_rooms"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    description = Column(String(300), default="")
    password_hash = Column(String(300), nullable=True)   # None = 공개방
    created_by_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.now)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(Integer, nullable=False, index=True)
    author_id = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
