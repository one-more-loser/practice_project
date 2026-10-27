"""联调诊断只记录计数和耗时，不保留SQL、参数或业务正文。"""
from contextvars import ContextVar
from time import perf_counter

from sqlalchemy import event
from sqlalchemy.engine import Engine

request_metrics = ContextVar("joint_request_metrics", default=None)


def before_query(connection, cursor, statement, parameters, context, executemany):
    if request_metrics.get() is not None:
        context.joint_started = perf_counter()


def after_query(connection, cursor, statement, parameters, context, executemany):
    metrics = request_metrics.get()
    if metrics is not None:
        metrics["queries"] += 1
        metrics["sql_ms"] += (perf_counter() - context.joint_started) * 1000


def install_metrics():
    if not event.contains(Engine, "before_cursor_execute", before_query):
        event.listen(Engine, "before_cursor_execute", before_query)
        event.listen(Engine, "after_cursor_execute", after_query)
