import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase, scoped_session

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

class Base(DeclarativeBase):
    pass

SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()