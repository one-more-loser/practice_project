from langgraph.checkpoint.postgres import PostgresSaver


class FencedPostgresSaver(PostgresSaver):
    """原生PG检查点加任务栅栏；只在短检查点读写期间锁会话，不跨模型调用。"""

    def __init__(self, connection, guard):
        super().__init__(connection)
        self.guard = guard

    def get_tuple(self, config):
        with self.guard():
            return super().get_tuple(config)

    def put(self, config, checkpoint, metadata, new_versions):
        with self.guard():
            return super().put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id, task_path=""):
        with self.guard():
            return super().put_writes(config, writes, task_id, task_path)
