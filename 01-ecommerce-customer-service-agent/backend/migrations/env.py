import os

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

from cs_agent.config import Settings
from cs_agent.db.models import Base
from cs_agent.db.session import verify_target

config = context.config
url = config.attributes.get("database_url") or os.environ.get("CS_MIGRATION_URL")
if not url:
    raise RuntimeError("CS_MIGRATION_URL required; offline migration execution disabled")
parsed = make_url(url)
settings = Settings(expected_database=parsed.database)
engine = create_engine(parsed, poolclass=pool.NullPool, hide_parameters=True)
try:
    verify_target(engine, settings, migration=True)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            include_object=lambda obj, name, type_, reflected, compare_to: (
                name != "cs_project_guard"
            ),
        )
        with context.begin_transaction():
            context.run_migrations()
finally:
    engine.dispose()
