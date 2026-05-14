from datetime import datetime

from sqlalchemy import Boolean, Column, Date, DateTime, Integer, String, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


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
    image = Column(String(500), nullable=True)        # 대표 이미지 파일명
    max_members = Column(Integer, nullable=True)      # 최대 팀 인원 (None=무제한)
    is_featured = Column(Boolean, default=False)
    submitted = Column(Boolean, default=False)        # 공모전 제출 여부
    submitted_at = Column(DateTime, nullable=True)    # 제출 기록 일시
    created_at = Column(DateTime, default=datetime.now)


class TeamMember(Base):
    __tablename__ = "team_members"

    id = Column(Integer, primary_key=True, index=True)
    competition_id = Column(Integer, nullable=False, index=True)
    nickname = Column(String(100), nullable=False)
    password_hash = Column(String(300), nullable=False)
    role = Column(String(50), default="기타")         # 기획/개발/디자인/마케팅/기타
    memo = Column(String(500), default="")
    is_leader = Column(Boolean, default=False)
    is_participant = Column(Boolean, default=False)   # 최종 제출 참여자로 기록됨
    member_id = Column(Integer, nullable=True)        # 연결된 Member.id (선택)
    created_at = Column(DateTime, default=datetime.now)


class Member(Base):
    """가입 회원 (초대 코드로 가입, 활동명 기반 로그인)"""
    __tablename__ = "members"

    id = Column(Integer, primary_key=True, index=True)
    activity_name = Column(String(100), unique=True, nullable=False, index=True)  # 활동명 (공개)
    real_name = Column(String(100), nullable=False)
    student_id = Column(String(50), default="")       # 학번 (관리자/중간관리자만 조회)
    phone = Column(String(50), default="")            # 전화번호 (관리자/중간관리자만 조회)
    password_hash = Column(String(300), nullable=False)
    bio = Column(Text, default="")
    profile_image = Column(String(500), nullable=True)
    role = Column(String(20), default="member")       # member / sub_admin
    invite_code_used = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class InviteCode(Base):
    """초대 코드 — 관리자가 발급하며 1회 사용 가능"""
    __tablename__ = "invite_codes"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(100), unique=True, nullable=False, index=True)
    note = Column(String(200), default="")            # 메모 (누구에게 줄 코드인지 등)
    created_at = Column(DateTime, default=datetime.now)
    expires_at = Column(DateTime, nullable=True)      # None이면 무기한
    used_by_member_id = Column(Integer, nullable=True)  # None이면 미사용
