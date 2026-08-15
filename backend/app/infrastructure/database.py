from collections.abc import Generator

from app.core.config import Settings
from app.infrastructure.models import Base
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


def create_database_engine(settings: Settings) -> Engine:
    database_url = settings.resolved_database_url()
    if database_url == "sqlite:///:memory:":
        return create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            future=True,
        )
    if database_url.startswith("sqlite"):
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False, "timeout": 30},
            future=True,
        )
        _configure_file_sqlite_engine(engine)
        return engine
    return create_engine(database_url, future=True)


def _configure_file_sqlite_engine(engine: Engine) -> None:
    """Make the development SQLite store safe for the API plus one coordinator."""

    @event.listens_for(engine, "connect")
    def configure_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def initialize_database(engine: Engine) -> None:
    Base.metadata.create_all(bind=engine)


def session_scope(factory: sessionmaker[Session]) -> Generator[Session]:
    with factory() as session:
        yield session
