import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./competitions.db")

# Railway PostgreSQL URL 호환
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _add_column_if_missing(conn, inspector, table: str, col: str, col_def: str):
    cols = [c["name"] for c in inspector.get_columns(table)]
    if col not in cols:
        conn.execute(__import__("sqlalchemy").text(f"ALTER TABLE {table} ADD COLUMN {col_def}"))


def init_db():
    Base.metadata.create_all(bind=engine)

    from sqlalchemy import inspect, text
    with engine.connect() as conn:
        inspector = inspect(engine)
        existing_tables = inspector.get_table_names()

        # ── competitions 신규 컬럼 ──────────────────────────────────────────────
        if "competitions" in existing_tables:
            _add_column_if_missing(conn, inspector, "competitions", "image",        "image VARCHAR(500)")
            _add_column_if_missing(conn, inspector, "competitions", "max_members",  "max_members INTEGER")
            _add_column_if_missing(conn, inspector, "competitions", "submitted",    "submitted BOOLEAN DEFAULT FALSE")
            _add_column_if_missing(conn, inspector, "competitions", "submitted_at", "submitted_at TIMESTAMP")

        # ── team_members 신규 컬럼 ─────────────────────────────────────────────
        if "team_members" in existing_tables:
            _add_column_if_missing(conn, inspector, "team_members", "is_participant", "is_participant BOOLEAN DEFAULT FALSE")
            _add_column_if_missing(conn, inspector, "team_members", "member_id",      "member_id INTEGER")

        conn.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
