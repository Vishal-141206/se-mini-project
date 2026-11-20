"""Database setup: SQLAlchemy engine, session factory, and declarative Base."""

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# SQLite database file stored in the project root
SQLALCHEMY_DATABASE_URL = "sqlite:///./orchestrator.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},  # required for SQLite + FastAPI threads
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """Yield a SQLAlchemy session and close it afterwards (FastAPI dependency)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
