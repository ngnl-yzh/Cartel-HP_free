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
    is_featured = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)
