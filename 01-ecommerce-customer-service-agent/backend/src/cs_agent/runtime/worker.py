import asyncio
import logging
import uuid
from contextlib import contextmanager

import psycopg
from langsmith import tracing_context
from psycopg.rows import dict_row
from sqlalchemy import select
from sqlalchemy.engine import make_url

from cs_agent.agent.graph import AgentGraph, history_messages
from cs_agent.config import Settings
from cs_agent.core.errors import AppError
from cs_agent.db.runtime_tables import messages
from cs_agent.models.gateway import ModelGateway
from cs_agent.query.executor import SQLExecutor
from cs_agent.query.service import QueryService
from cs_agent.runtime.checkpoints import FencedPostgresSaver
from cs_agent.runtime.leases import RuntimeService

logger = logging.getLogger("cs_agent.runtime.worker")


RETRYABLE = {
    "model_timeout",
    "model_unavailable",
    "model_busy",
    "query_timeout",
    "query_unavailable",
}


class AgentWorker:
    def __init__(self, database, settings: Settings, clock, *, gateway=None):
        if not settings.database_url:
            raise RuntimeError("agent worker database unavailable")
        self.database, self.settings, self.clock = database, settings, clock
        self.runtime = RuntimeService()
        self.executor_id = "worker_" + uuid.uuid4().hex[:16]
        self.gateway = gateway or ModelGateway.from_settings(settings)
        self.sql_executor = SQLExecutor(settings) if settings.query_database_url else None
        self.query_service = QueryService(self.sql_executor)
        url = make_url(settings.database_url.get_secret_value())
        self.checkpoint_connection = psycopg.connect(
            url.set(drivername="postgresql").render_as_string(hide_password=False),
            autocommit=True,
            row_factory=dict_row,
            connect_timeout=3,
            options="-c search_path=lg,public -c timezone=UTC",
        )
        exists = self.checkpoint_connection.execute(
            "SELECT to_regclass('checkpoint_migrations') IS NOT NULL AS ready"
        ).fetchone()["ready"]
        if not exists:
            self.checkpoint_connection.close()
            if self.sql_executor:
                self.sql_executor.close()
            raise RuntimeError("postgres checkpoints are not prepared")

    @contextmanager
    def _saver_guard(self, lease):
        with self.runtime.guard(self.database, lease, self.clock):
            yield

    def process_once(self):
        lease = self.runtime.claim(self.database, self.executor_id, self.clock.now())
        if lease is None:
            return False
        # 禁用环境中意外启用的外部追踪，模型上下文/检查点不外发给追踪平台。
        with tracing_context(enabled=False):
            return self._process(lease)

    def _process(self, lease):
        try:
            saver = FencedPostgresSaver(
                self.checkpoint_connection, lambda: self._saver_guard(lease)
            )
            graph_runner = AgentGraph(
                self.database,
                lease,
                self.runtime,
                self.gateway,
                saver,
                self.clock,
                self.query_service,
            )
            graph = graph_runner.compile()
            config = {"configurable": {"thread_id": lease.run_id}}
            snapshot = graph.get_state(config)
            if snapshot.values and snapshot.next:
                output = graph.invoke(None, config=config)
            elif snapshot.values:
                output = snapshot.values
            else:
                with self.database.transaction() as session:
                    conversation, run, _ = self.runtime.verify(session, lease, self.clock.now())
                    rows = (
                        session.execute(
                            select(messages)
                            .where(messages.c.conversation_id == lease.conversation_id)
                            .order_by(messages.c.message_seq)
                        )
                        .mappings()
                        .all()
                    )
                    initial = {
                        "messages": history_messages(rows),
                        "dialog_state": conversation.get("dialog_state") or {},
                        "tool_rounds": 0,
                        "tool_results": [],
                        "citations": [],
                    }
                output = graph.invoke(initial, config=config)
            self.runtime.finish(
                self.database,
                lease,
                self.clock,
                output.get("answer", "目前无法完成这次咨询，请稍后重试。"),
                output.get("dialog_state", {}),
                waiting=bool(output.get("waiting")),
                citations=output.get("citations", []),
            )
            return True
        except AppError as exc:
            self._finish_failure(lease, exc.code, exc.code in RETRYABLE)
            return True
        except Exception:
            logger.error("agent_task_failed run_id=%s", lease.run_id)
            self._finish_failure(lease, "agent_internal_error", False)
            return True

    def _finish_failure(self, lease, code, retryable):
        try:
            self.runtime.fail(self.database, lease, self.clock, code, retryable=retryable)
        except Exception:
            logger.error("agent_failure_state_unavailable run_id=%s", lease.run_id)

    async def run(self, stop_event: asyncio.Event):
        while not stop_event.is_set():
            try:
                worked = await asyncio.to_thread(self.process_once)
            except Exception:
                logger.error("agent_worker_poll_failed")
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=0.75)
                except TimeoutError:
                    pass

    def close(self):
        self.checkpoint_connection.close()
        if self.sql_executor:
            self.sql_executor.close()
