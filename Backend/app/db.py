from typing import Annotated, Any
from sqlmodel import create_engine, Session
from fastapi import Depends
from app.config import Settings, settings


def engine_options(config: Settings) -> dict[str, Any]:
    """Engine settings that turn a dead connection or a stuck lock wait into
    an ``OperationalError`` instead of an indefinite hang."""
    return {
        "pool_size": config.database_pool_size,
        "max_overflow": config.database_max_overflow,
        # Replace pooled connections that died (e.g. a Postgres restart)
        # before handing them out.
        "pool_pre_ping": True,
        "connect_args": {
            "connect_timeout": config.database_connect_timeout,
            # A half-open connection is detected after about
            # idle + interval * count seconds instead of the OS default
            # (about two hours on Linux).
            "keepalives": 1,
            "keepalives_idle": config.database_keepalives_idle,
            "keepalives_interval": config.database_keepalives_interval,
            "keepalives_count": config.database_keepalives_count,
            # Bounds waiting on another session's lock without capping how
            # long a statement may run once it holds its locks.
            "options": f"-c lock_timeout={config.database_lock_timeout_ms}",
        },
    }


engine = create_engine(settings.database_url, **engine_options(settings))

def get_session():
    with Session(engine) as session:
        yield session

SessionDep = Annotated[Session, Depends(get_session)]
