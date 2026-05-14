import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./competitions.db")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    Base.metadata.create_all(bind=engine)
    # 기존 DB 마이그레이션 — 새 컬럼이 없으면 추가
    from sqlalchemy import inspect, text
    with engine.connect() as conn:
        inspector = inspect(engine)
        comp_cols = [c["name"] for c in inspector.get_columns("competitions")]
        if "image" not in comp_cols:
            conn.execute(text("ALTER TABLE competitions ADD COLUMN image VARCHAR(500)"))
        if "max_members" not in comp_cols:
            conn.execute(text("ALTER TABLE competitions ADD COLUMN max_members INTEGER"))
        conn.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
