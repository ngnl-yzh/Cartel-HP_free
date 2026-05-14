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
    image = Column(String(500), nullable=True)       # 대표 이미지 파일명
    max_members = Column(Integer, nullable=True)     # 최대 팀 인원 (None=무제한)
    is_featured = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)


class TeamMember(Base):
    __tablename__ = "team_members"

    id = Column(Integer, primary_key=True, index=True)
    competition_id = Column(Integer, nullable=False, index=True)
    nickname = Column(String(100), nullable=False)
    password_hash = Column(String(300), nullable=False)
    role = Column(String(50), default="기타")       # 기획 / 개발 / 디자인 / 마케팅 / 기타
    memo = Column(String(500), default="")
    is_leader = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)
