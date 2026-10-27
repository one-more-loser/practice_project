from sqlalchemy import ForeignKeyConstraint, Integer, String, UniqueConstraint

from cs_agent.db.business_tables import business_table, col

bindings = business_table(
    "agent_action_binding",
    col("binding_id", primary_key=True),
    col("confirmation_id"),
    col("conversation_id"),
    col("run_id"),
    col("mode_epoch", Integer),
    col("request_digest", String(64)),
    constraints=(
        ForeignKeyConstraint(["confirmation_id"], ["action_confirmation.confirmation_id"]),
        ForeignKeyConstraint(["conversation_id"], ["conversation.conversation_id"]),
        ForeignKeyConstraint(["run_id"], ["agent_run.run_id"]),
        UniqueConstraint("confirmation_id"),
        UniqueConstraint("run_id", "request_digest"),
    ),
)
