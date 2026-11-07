from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

engine = None
SessionLocal: sessionmaker | None = None


def configure(url: str):
    global engine, SessionLocal
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    kw = {}
    if url.startswith("sqlite"):
        kw = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url:
            kw["poolclass"] = StaticPool
    else:
        kw = {"pool_pre_ping": True}
    engine = create_engine(url, **kw)
    if url.startswith("sqlite"):
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):          # SQLite ignores foreign keys unless told otherwise
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    return engine


def get_db():
    """Routes commit explicitly. This only guarantees the session is closed."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
