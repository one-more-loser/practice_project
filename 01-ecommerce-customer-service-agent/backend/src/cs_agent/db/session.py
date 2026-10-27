import json
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from fastapi import Request
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import sessionmaker

from cs_agent.config import PROJECT_ROOT, Settings


def verify_target(engine: Engine, settings: Settings, *, migration=False, query=False):
    """迁移/应用均核对项目标识；不接受任意URL或旧工程数据库。"""
    path = settings.isolation_file.resolve()
    if not path.is_relative_to(PROJECT_ROOT / ".runtime"):
        raise RuntimeError("isolation file outside project")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    kind = manifest.get("kind", "windows_local")
    if kind not in {"windows_local", "docker_debian"}:
        raise RuntimeError("unknown database isolation kind")
    host = manifest.get("host", "127.0.0.1")
    server_port = manifest.get("server_port", manifest["port"])
    if kind == "windows_local" and (host != "127.0.0.1" or server_port != manifest["port"]):
        raise RuntimeError("invalid local isolation target")
    url = engine.url
    role = manifest["owner_role"] if migration else manifest["app_role"]
    if query:
        role = manifest.get("query_role")
        if not role or migration:
            raise RuntimeError("query role not configured")
    if (
        url.host != host
        or url.port != manifest["port"]
        or url.database != settings.expected_database
        or url.database not in manifest["databases"]
        or url.username != role
        or Path(manifest["project_root"]).resolve() != PROJECT_ROOT
    ):
        raise RuntimeError("database isolation mismatch")
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT current_database(), current_user, inet_server_port(), "
                "(SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user), "
                "(SELECT instance_id FROM cs_project_guard) "
            )
        ).one()
        if tuple(row) != (url.database, role, server_port, False, manifest["instance_id"]):
            raise RuntimeError("database identity mismatch")
        if migration:
            actual = conn.execute(
                text("SELECT setting FROM pg_settings WHERE name='data_directory'")
            ).scalar_one()
            expected = manifest.get("data_directory", "/var/lib/postgresql/data")
            matches = (
                (Path(actual).resolve() == (PROJECT_ROOT / ".runtime/pgdata").resolve())
                if kind == "windows_local"
                else (PurePosixPath(actual) == PurePosixPath(expected))
            )
            if not matches:
                raise RuntimeError("database directory mismatch")


class Database:
    def __init__(self, settings: Settings):
        if settings.database_url is None:
            raise RuntimeError("database unconfigured")
        url = make_url(settings.database_url.get_secret_value())
        if url.drivername != "postgresql+psycopg":
            raise RuntimeError("PostgreSQL psycopg required")
        self.engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=8,
            max_overflow=0,
            pool_timeout=3,
            echo=False,
            hide_parameters=True,
            connect_args={
                "connect_timeout": 3,
                "options": "-c statement_timeout=2000 -c timezone=UTC",
            },
        )
        verify_target(self.engine, settings)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def transaction(self):
        with self.sessions.begin() as session:
            yield session

    def close(self):
        self.engine.dispose()


def session_dependency(request: Request):
    from cs_agent.core.errors import AppError

    db = request.app.state.db
    if db is None:
        raise AppError("unavailable", 503, "数据库尚未就绪")
    with db.transaction() as session:
        yield session
