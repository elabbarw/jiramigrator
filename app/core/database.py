"""Database configuration and session management."""
import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Database URL - SQLite for simplicity, can be changed to PostgreSQL
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./jira_migration.db")

# Create engine
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL, 
        connect_args={"check_same_thread": False}
    )
else:
    engine = create_engine(DATABASE_URL)

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for models
Base = declarative_base()


def get_db():
    """Dependency for getting database sessions."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Initialize database tables."""
    from app.models import job, configuration  # noqa: F401
    Base.metadata.create_all(bind=engine)
