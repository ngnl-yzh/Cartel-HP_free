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


def _add_col(conn, inspector, table: str, col: str, col_def: str):
    cols = [c["name"] for c in inspector.get_columns(table)]
    if col not in cols:
        from sqlalchemy import text
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_def}"))


def init_db():
    Base.metadata.create_all(bind=engine)

    from sqlalchemy import inspect
    with engine.connect() as conn:
        inspector = inspect(engine)
        tables = inspector.get_table_names()

        if "competitions" in tables:
            _add_col(conn, inspector, "competitions", "image",        "image VARCHAR(500)")
            _add_col(conn, inspector, "competitions", "max_members",  "max_members INTEGER")
            _add_col(conn, inspector, "competitions", "submitted",    "submitted BOOLEAN DEFAULT FALSE")
            _add_col(conn, inspector, "competitions", "submitted_at", "submitted_at TIMESTAMP")

        if "team_members" in tables:
            _add_col(conn, inspector, "team_members", "is_participant", "is_participant BOOLEAN DEFAULT FALSE")
            _add_col(conn, inspector, "team_members", "member_id",      "member_id INTEGER")

        conn.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
