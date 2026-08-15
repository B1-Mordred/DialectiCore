from app.core.config import Settings
from app.infrastructure.database import create_database_engine
from sqlalchemy import text


def test_file_sqlite_engine_uses_wal_and_busy_timeout(tmp_path) -> None:
    engine = create_database_engine(
        Settings(database_url=f"sqlite:///{tmp_path / 'dialecticore.db'}")
    )

    with engine.connect() as connection:
        journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
        busy_timeout = connection.execute(text("PRAGMA busy_timeout")).scalar_one()

    assert journal_mode == "wal"
    assert busy_timeout == 30_000
