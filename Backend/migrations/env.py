from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.models.metadata import metadata
from app.schema_revision import database_url, run_migrations


config = context.config

# python -m app.migrate and tests configure logging themselves.
if config.config_file_name is not None and config.attributes.get(
    "configure_logger", True
):
    fileConfig(config.config_file_name)

target_metadata = metadata


def include_object(object_, name, type_, reflected, compare_to):
    # VECTOR_INDEX=hnsw is an opt-in index created by hand, not part of the
    # schema the migrations own (Backend/README.md).
    if type_ == "index" and reflected and name == "ix_embeddings_hnsw":
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    def migrate(connection) -> None:
        def configure_and_run() -> None:
            # The connection is already in run_migrations' transaction, so
            # Alembic treats it as external and leaves the commit to it.
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                include_object=include_object,
            )
            with context.begin_transaction():
                context.run_migrations()

        run_migrations(connection, configure_and_run)

    # Tests pass their own connection; it must not be in a transaction.
    connection = config.attributes.get("connection")
    if connection is not None:
        migrate(connection)
        return
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            migrate(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
