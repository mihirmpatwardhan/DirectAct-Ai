from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import inspect, text
from app.core.config import settings

engine = create_async_engine(
    settings.database_url,
    # SQL echo is extremely noisy and emits emoji encoding errors on the
    # Windows CP1252 console. Keep it opt-in for explicit DEBUG log level.
    echo=settings.debug and settings.log_level.lower() == "debug",
    future=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    """Dependency: yield an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    """Create tables and apply small compatibility migrations on startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_apply_compatibility_migrations)


def _apply_compatibility_migrations(connection) -> None:
    """Bring databases created by older versions up to the current schema.

    ``create_all`` only creates missing tables; it does not add columns to
    tables that already exist. Keep these migrations idempotent so application
    startup is safe for both fresh and existing SQLite databases.
    """
    sessions_columns = {
        column["name"] for column in inspect(connection).get_columns("sessions")
    }
    if "user_id" not in sessions_columns:
        connection.execute(text(
            "ALTER TABLE sessions ADD COLUMN user_id VARCHAR(36)"
        ))
