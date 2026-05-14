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
    pool_pre_ping=True,
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

        # teams 테이블은 create_all로 자동 생성됨

        if "team_members" in tables:
            _add_col(conn, inspector, "team_members", "is_participant", "is_participant BOOLEAN DEFAULT FALSE")
            _add_col(conn, inspector, "team_members", "member_id",      "member_id INTEGER")
            _add_col(conn, inspector, "team_members", "team_id",        "team_id INTEGER")
            _add_col(conn, inspector, "team_members", "award_rank",     "award_rank VARCHAR(50)")
            _add_col(conn, inspector, "team_members", "award_prize",    "award_prize VARCHAR(300) DEFAULT ''")
            _add_col(conn, inspector, "team_members", "award_note",     "award_note TEXT DEFAULT ''")

        # 기존 TeamMember(team_id=NULL) 데이터를 위해 기본 팀 생성
        from sqlalchemy import text as _t
        rows = conn.execute(_t(
            "SELECT DISTINCT competition_id FROM team_members WHERE team_id IS NULL AND competition_id IS NOT NULL"
        )).fetchall()
        for (cid,) in rows:
            existing = conn.execute(_t(
                "SELECT id FROM teams WHERE competition_id = :cid LIMIT 1"
            ), {"cid": cid}).fetchone()
            if existing:
                tid = existing[0]
            else:
                conn.execute(_t(
                    "INSERT INTO teams (competition_id, name, description, submitted, created_at) "
                    "VALUES (:cid, '기본 팀', '', FALSE, CURRENT_TIMESTAMP)"
                ), {"cid": cid})
                tid = conn.execute(_t(
                    "SELECT id FROM teams WHERE competition_id = :cid ORDER BY id DESC LIMIT 1"
                ), {"cid": cid}).fetchone()[0]
            conn.execute(_t(
                "UPDATE team_members SET team_id = :tid WHERE competition_id = :cid AND team_id IS NULL"
            ), {"tid": tid, "cid": cid})

        conn.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
