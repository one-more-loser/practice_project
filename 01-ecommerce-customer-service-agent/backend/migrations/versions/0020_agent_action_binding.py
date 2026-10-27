"""会话提案绑定与人工接管失效；冻结DDL。"""

from alembic import op

revision = "0020_agent_action_binding"
down_revision = "0019_experience_feedback"


def upgrade():
    op.execute(
        "\nCREATE TABLE agent_action_binding (\n\tbinding_id VARCHAR(80) NOT NULL, \n\tconfirmation_id VARCHAR(80) NOT NULL, \n\tconversation_id VARCHAR(80) NOT NULL, \n\trun_id VARCHAR(80) NOT NULL, \n\tmode_epoch INTEGER NOT NULL, \n\trequest_digest VARCHAR(64) NOT NULL, \n\tstore_id VARCHAR(80) NOT NULL, \n\tsource_kind VARCHAR(32) NOT NULL, \n\tdata_batch_id VARCHAR(80) NOT NULL, \n\tbatch_generation INTEGER NOT NULL, \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (binding_id), \n\tFOREIGN KEY(confirmation_id) REFERENCES action_confirmation (confirmation_id), \n\tFOREIGN KEY(conversation_id) REFERENCES conversation (conversation_id), \n\tFOREIGN KEY(run_id) REFERENCES agent_run (run_id), \n\tUNIQUE (confirmation_id), \n\tUNIQUE (run_id, request_digest), \n\tFOREIGN KEY(store_id) REFERENCES store (store_id), \n\tFOREIGN KEY(data_batch_id, batch_generation) REFERENCES source_batch (batch_id, generation), \n\tCHECK (version > 0), \n\tCHECK (source_kind IN ('synthetic','derived','business_import'))\n)\n\n;\nGRANT SELECT, INSERT ON agent_action_binding TO cs_agent_app_b0;"
    )


def downgrade():
    raise RuntimeError("提案审计不自动回退")
