import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import URL

from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.db.session import verify_target


@pytest.fixture(autouse=True)
def isolated_guard_project(tmp_path, monkeypatch):
    # 测试只替换项目根常量；生产verify_target仍执行原有路径、实例和角色检查。
    monkeypatch.setitem(globals(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("cs_agent.db.session.PROJECT_ROOT", tmp_path)
    (tmp_path / ".runtime").mkdir()


def remote_target(
    tmp_path,
    *,
    host="192.168.10.100",
    directory="/var/lib/postgresql/data",
    identity="deployment-test",
):
    # 清单限本例临时项目.runtime；不复用现有运行清单，不向真实数据库发送SQL。
    folder = PROJECT_ROOT / ".runtime/target-unit-tests"
    folder.mkdir(exist_ok=True)
    path = folder / (tmp_path.name + ".json")
    path.write_text(
        json.dumps(
            {
                "kind": "docker_debian",
                "host": "192.168.10.100",
                "port": 55432,
                "server_port": 5432,
                "data_directory": "/var/lib/postgresql/data",
                "project_root": str(PROJECT_ROOT),
                "databases": ["cs_agent_b0", "cs_agent_b0_test", "cs_agent_b0_upgrade"],
                "owner_role": "cs_agent_owner_b0",
                "app_role": "cs_agent_app_b0",
                "instance_id": "deployment-test",
            }
        ),
        encoding="utf-8",
    )

    class Connection:
        def execute(self, statement):
            return SimpleNamespace(
                one=lambda: ("cs_agent_b0", "cs_agent_owner_b0", 5432, False, identity),
                scalar_one=lambda: directory,
            )

    class Engine:
        url = URL.create(
            "postgresql+psycopg",
            username="cs_agent_owner_b0",
            host=host,
            port=55432,
            database="cs_agent_b0",
        )

        @contextmanager
        def connect(self):
            yield Connection()

    return Engine(), Settings(isolation_file=path, expected_database="cs_agent_b0")


def test_remote_container_distinguishes_published_and_server_ports(tmp_path):
    engine, settings = remote_target(tmp_path)
    verify_target(engine, settings, migration=True)


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "192.168.10.101"},
        {"directory": "/another-project/data"},
        {"identity": "old-project"},
    ],
)
def test_remote_target_rejects_wrong_endpoint_directory_or_instance(tmp_path, changes):
    engine, settings = remote_target(tmp_path, **changes)
    with pytest.raises(RuntimeError):
        verify_target(engine, settings, migration=True)
