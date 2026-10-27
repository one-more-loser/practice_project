"""持久会话/任务/事件DDL快照；LangGraph原生表由独立setup命令准备。"""

from alembic import op

revision = "0013_conversation_runtime"
down_revision = "0012_query_scope"


def upgrade():
    op.execute(
        "\nCREATE TABLE conversation (\n\tconversation_id VARCHAR(80) NOT NULL, \n\towner_id VARCHAR(80) NOT NULL, \n\tprincipal_kind VARCHAR(16) NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tmode VARCHAR(16) NOT NULL, \n\tmode_epoch INTEGER NOT NULL, \n\tlast_seq INTEGER NOT NULL, \n\tfloor_seq INTEGER NOT NULL, \n\tactive_run_id VARCHAR(80), \n\tdialog_state JSONB NOT NULL, \n\tstore_id VARCHAR(80) NOT NULL, \n\tsource_kind VARCHAR(32) NOT NULL, \n\tdata_batch_id VARCHAR(80) NOT NULL, \n\tbatch_generation INTEGER NOT NULL, \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (conversation_id), \n\tFOREIGN KEY(store_id) REFERENCES store (store_id), \n\tFOREIGN KEY(data_batch_id, batch_generation) REFERENCES source_batch (batch_id, generation), \n\tCHECK (version > 0), \n\tCHECK (source_kind IN ('synthetic','derived','business_import'))\n)\n\n"
    )
    op.execute("CREATE INDEX ix_conversation_owner ON conversation (owner_id, store_id)")
    op.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON "conversation" TO cs_agent_app_b0')
    op.execute(
        "\nCREATE TABLE guest_session (\n\tguest_id VARCHAR(80) NOT NULL, \n\ttoken_digest VARCHAR(64) NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tstore_id VARCHAR(80) NOT NULL, \n\tsource_kind VARCHAR(32) NOT NULL, \n\tdata_batch_id VARCHAR(80) NOT NULL, \n\tbatch_generation INTEGER NOT NULL, \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (guest_id), \n\tUNIQUE (token_digest), \n\tFOREIGN KEY(store_id) REFERENCES store (store_id), \n\tFOREIGN KEY(data_batch_id, batch_generation) REFERENCES source_batch (batch_id, generation), \n\tCHECK (version > 0), \n\tCHECK (source_kind IN ('synthetic','derived','business_import'))\n)\n\n"
    )
    op.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON "guest_session" TO cs_agent_app_b0')
    op.execute(
        "\nCREATE TABLE conversation_event (\n\tevent_id VARCHAR(80) NOT NULL, \n\tconversation_id VARCHAR(80) NOT NULL, \n\tstream_id VARCHAR(80) NOT NULL, \n\tseq INTEGER NOT NULL, \n\tmode_epoch INTEGER NOT NULL, \n\ttype VARCHAR(48) NOT NULL, \n\tdata JSONB NOT NULL, \n\tstore_id VARCHAR(80) NOT NULL, \n\tsource_kind VARCHAR(32) NOT NULL, \n\tdata_batch_id VARCHAR(80) NOT NULL, \n\tbatch_generation INTEGER NOT NULL, \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (event_id), \n\tFOREIGN KEY(conversation_id) REFERENCES conversation (conversation_id), \n\tUNIQUE (stream_id, seq), \n\tFOREIGN KEY(store_id) REFERENCES store (store_id), \n\tFOREIGN KEY(data_batch_id, batch_generation) REFERENCES source_batch (batch_id, generation), \n\tCHECK (version > 0), \n\tCHECK (source_kind IN ('synthetic','derived','business_import'))\n)\n\n"
    )
    op.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON "conversation_event" TO cs_agent_app_b0')
    op.execute(
        "\nCREATE TABLE conversation_message (\n\tmessage_id VARCHAR(80) NOT NULL, \n\tconversation_id VARCHAR(80) NOT NULL, \n\tsource_role VARCHAR(16) NOT NULL, \n\ttext VARCHAR(6000) NOT NULL, \n\tclient_message_id VARCHAR(128) NOT NULL, \n\tcontent_digest VARCHAR(64) NOT NULL, \n\trun_id VARCHAR(80), \n\tstatus VARCHAR(24) NOT NULL, \n\tstore_id VARCHAR(80) NOT NULL, \n\tsource_kind VARCHAR(32) NOT NULL, \n\tdata_batch_id VARCHAR(80) NOT NULL, \n\tbatch_generation INTEGER NOT NULL, \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (message_id), \n\tFOREIGN KEY(conversation_id) REFERENCES conversation (conversation_id), \n\tUNIQUE (conversation_id, source_role, client_message_id), \n\tFOREIGN KEY(store_id) REFERENCES store (store_id), \n\tFOREIGN KEY(data_batch_id, batch_generation) REFERENCES source_batch (batch_id, generation), \n\tCHECK (version > 0), \n\tCHECK (source_kind IN ('synthetic','derived','business_import'))\n)\n\n"
    )
    op.execute(
        "CREATE INDEX ix_message_history ON conversation_message (conversation_id, created_at)"
    )
    op.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON "conversation_message" TO cs_agent_app_b0')
    op.execute(
        "\nCREATE TABLE agent_run (\n\trun_id VARCHAR(80) NOT NULL, \n\tconversation_id VARCHAR(80) NOT NULL, \n\tmessage_id VARCHAR(80) NOT NULL, \n\towner_id VARCHAR(80) NOT NULL, \n\tsession_ref VARCHAR(80) NOT NULL, \n\tauth_version INTEGER NOT NULL, \n\tprincipal_kind VARCHAR(16) NOT NULL, \n\tcontext_kind VARCHAR(24) NOT NULL, \n\tmode_epoch INTEGER NOT NULL, \n\tstatus VARCHAR(24) NOT NULL, \n\tgraph_version VARCHAR(40) NOT NULL, \n\tcheckpoint_thread VARCHAR(80) NOT NULL, \n\terror_code VARCHAR(48), \n\tstore_id VARCHAR(80) NOT NULL, \n\tsource_kind VARCHAR(32) NOT NULL, \n\tdata_batch_id VARCHAR(80) NOT NULL, \n\tbatch_generation INTEGER NOT NULL, \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (run_id), \n\tFOREIGN KEY(conversation_id) REFERENCES conversation (conversation_id), \n\tFOREIGN KEY(message_id) REFERENCES conversation_message (message_id), \n\tUNIQUE (message_id), \n\tFOREIGN KEY(store_id) REFERENCES store (store_id), \n\tFOREIGN KEY(data_batch_id, batch_generation) REFERENCES source_batch (batch_id, generation), \n\tCHECK (version > 0), \n\tCHECK (source_kind IN ('synthetic','derived','business_import'))\n)\n\n"
    )
    op.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON "agent_run" TO cs_agent_app_b0')
    op.execute(
        "\nCREATE TABLE agent_task (\n\ttask_id VARCHAR(80) NOT NULL, \n\trun_id VARCHAR(80) NOT NULL, \n\tstatus VARCHAR(24) NOT NULL, \n\tfence INTEGER NOT NULL, \n\texecutor_id VARCHAR(80), \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tattempts INTEGER NOT NULL, \n\tstore_id VARCHAR(80) NOT NULL, \n\tsource_kind VARCHAR(32) NOT NULL, \n\tdata_batch_id VARCHAR(80) NOT NULL, \n\tbatch_generation INTEGER NOT NULL, \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (task_id), \n\tFOREIGN KEY(run_id) REFERENCES agent_run (run_id), \n\tUNIQUE (run_id), \n\tFOREIGN KEY(store_id) REFERENCES store (store_id), \n\tFOREIGN KEY(data_batch_id, batch_generation) REFERENCES source_batch (batch_id, generation), \n\tCHECK (version > 0), \n\tCHECK (source_kind IN ('synthetic','derived','business_import'))\n)\n\n"
    )
    op.execute("CREATE INDEX ix_runtime_claim ON agent_task (status, lease_until)")
    op.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON "agent_task" TO cs_agent_app_b0')
    op.execute("CREATE SCHEMA IF NOT EXISTS lg")
    op.execute("REVOKE ALL ON SCHEMA lg FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA lg TO cs_agent_app_b0")


def downgrade():
    raise RuntimeError("会话历史不自动破坏性回退")
