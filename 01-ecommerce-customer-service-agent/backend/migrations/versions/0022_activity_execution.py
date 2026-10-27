"""活动执行slot、额度、可靠投递；冻结DDL，不导入运行时metadata。"""

from alembic import op

revision = "0022_activity_execution"
down_revision = "0021_operations_capabilities"


def upgrade():
    op.execute(
        "\nCREATE TABLE activity_preview (\n\tpreview_ref VARCHAR(80) NOT NULL, \n\tactor_ref VARCHAR(80) NOT NULL, \n\tauth_version INTEGER NOT NULL, \n\tcapability_revision INTEGER NOT NULL, \n\tplan_id VARCHAR(80) NOT NULL, \n\tplan_version INTEGER NOT NULL, \n\tmanifest_digest VARCHAR(64) NOT NULL, \n\tmanifest JSONB NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tstore_id VARCHAR(80) NOT NULL, \n\tsource_kind VARCHAR(32) NOT NULL, \n\tdata_batch_id VARCHAR(80) NOT NULL, \n\tbatch_generation INTEGER NOT NULL, \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (preview_ref), \n\tFOREIGN KEY(store_id) REFERENCES store (store_id), \n\tFOREIGN KEY(data_batch_id, batch_generation) REFERENCES source_batch (batch_id, generation), \n\tCHECK (version > 0), \n\tCHECK (source_kind IN ('synthetic','derived','business_import'))\n)\n\n"
    )
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON "activity_preview" TO cs_agent_app_b0')
    op.execute(
        "\nCREATE TABLE activity_execution_slot (\n\tintent_id VARCHAR(80) NOT NULL, \n\tplan_id VARCHAR(80) NOT NULL, \n\tplan_version INTEGER NOT NULL, \n\tapproval_ref VARCHAR(80) NOT NULL, \n\tmanifest_digest VARCHAR(64) NOT NULL, \n\tmanifest JSONB NOT NULL, \n\tactor_ref VARCHAR(80) NOT NULL, \n\tactor_role VARCHAR(32) NOT NULL, \n\tsession_ref VARCHAR(80) NOT NULL, \n\tauth_version INTEGER NOT NULL, \n\tcapability_revision INTEGER NOT NULL, \n\tpreview_ref VARCHAR(80) NOT NULL, \n\tgrant_ref VARCHAR(80), \n\tgrant_expires_at TIMESTAMP WITH TIME ZONE, \n\tstatus VARCHAR(24) NOT NULL, \n\tactivity_id VARCHAR(80), \n\treceipt JSONB, \n\tstore_id VARCHAR(80) NOT NULL, \n\tsource_kind VARCHAR(32) NOT NULL, \n\tdata_batch_id VARCHAR(80) NOT NULL, \n\tbatch_generation INTEGER NOT NULL, \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (intent_id), \n\tUNIQUE (store_id, data_batch_id, batch_generation, plan_id, plan_version, approval_ref), \n\tUNIQUE (store_id, data_batch_id, batch_generation, approval_ref), \n\tCHECK (status IN ('pending','committed','closed_no_effect')), \n\tFOREIGN KEY(store_id) REFERENCES store (store_id), \n\tFOREIGN KEY(data_batch_id, batch_generation) REFERENCES source_batch (batch_id, generation), \n\tCHECK (version > 0), \n\tCHECK (source_kind IN ('synthetic','derived','business_import'))\n)\n\n"
    )
    op.execute(
        'GRANT SELECT, INSERT, UPDATE, DELETE ON "activity_execution_slot" TO cs_agent_app_b0'
    )
    op.execute(
        "\nCREATE TABLE activity_execution (\n\tactivity_id VARCHAR(80) NOT NULL, \n\tintent_id VARCHAR(80) NOT NULL, \n\tmanifest JSONB NOT NULL, \n\tphase VARCHAR(16) NOT NULL, \n\trevision INTEGER NOT NULL, \n\tstart_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tend_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tvariable_cap_minor BIGINT NOT NULL, \n\treserved_discount_minor BIGINT NOT NULL, \n\tconsumed_discount_minor BIGINT NOT NULL, \n\tactual_revision INTEGER NOT NULL, \n\tsnapshot_id VARCHAR(80) NOT NULL, \n\twatermark TIMESTAMP WITH TIME ZONE NOT NULL, \n\tstore_id VARCHAR(80) NOT NULL, \n\tsource_kind VARCHAR(32) NOT NULL, \n\tdata_batch_id VARCHAR(80) NOT NULL, \n\tbatch_generation INTEGER NOT NULL, \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (activity_id), \n\tUNIQUE (intent_id), \n\tFOREIGN KEY(intent_id) REFERENCES activity_execution_slot (intent_id), \n\tCHECK (phase IN ('scheduled','active','paused','ended') AND revision>0 AND start_at<end_at), \n\tCHECK (variable_cap_minor>=0 AND reserved_discount_minor>=0 AND consumed_discount_minor>=0 AND reserved_discount_minor+consumed_discount_minor<=variable_cap_minor), \n\tFOREIGN KEY(store_id) REFERENCES store (store_id), \n\tFOREIGN KEY(data_batch_id, batch_generation) REFERENCES source_batch (batch_id, generation), \n\tCHECK (version > 0), \n\tCHECK (source_kind IN ('synthetic','derived','business_import'))\n)\n\n"
    )
    op.execute(
        "CREATE INDEX ix_activity_window ON activity_execution (store_id, data_batch_id, batch_generation, phase, end_at)"
    )
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON "activity_execution" TO cs_agent_app_b0')
    op.execute(
        "\nCREATE TABLE activity_execution_line (\n\tline_id VARCHAR(80) NOT NULL, \n\tactivity_id VARCHAR(80) NOT NULL, \n\tsku_id VARCHAR(80) NOT NULL, \n\tbase_price_minor BIGINT NOT NULL, \n\teffective_price_minor BIGINT NOT NULL, \n\tprice_version INTEGER NOT NULL, \n\tquantity_cap BIGINT NOT NULL, \n\treserved_qty BIGINT NOT NULL, \n\tconsumed_qty BIGINT NOT NULL, \n\tstore_id VARCHAR(80) NOT NULL, \n\tsource_kind VARCHAR(32) NOT NULL, \n\tdata_batch_id VARCHAR(80) NOT NULL, \n\tbatch_generation INTEGER NOT NULL, \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (line_id), \n\tUNIQUE (activity_id, sku_id), \n\tFOREIGN KEY(activity_id) REFERENCES activity_execution (activity_id), \n\tFOREIGN KEY(sku_id) REFERENCES sku (sku_id), \n\tCHECK (quantity_cap>0 AND reserved_qty>=0 AND consumed_qty>=0 AND reserved_qty+consumed_qty<=quantity_cap), \n\tCHECK (effective_price_minor>0 AND base_price_minor>=effective_price_minor AND price_version>0), \n\tFOREIGN KEY(store_id) REFERENCES store (store_id), \n\tFOREIGN KEY(data_batch_id, batch_generation) REFERENCES source_batch (batch_id, generation), \n\tCHECK (version > 0), \n\tCHECK (source_kind IN ('synthetic','derived','business_import'))\n)\n\n"
    )
    op.execute(
        "CREATE INDEX ix_activity_sku ON activity_execution_line (store_id, data_batch_id, batch_generation, sku_id)"
    )
    op.execute(
        'GRANT SELECT, INSERT, UPDATE, DELETE ON "activity_execution_line" TO cs_agent_app_b0'
    )
    op.execute(
        "\nCREATE TABLE activity_order_allocation (\n\tallocation_id VARCHAR(80) NOT NULL, \n\tactivity_id VARCHAR(80) NOT NULL, \n\torder_id VARCHAR(80) NOT NULL, \n\tsku_id VARCHAR(80) NOT NULL, \n\tqty BIGINT NOT NULL, \n\tdiscount_minor BIGINT NOT NULL, \n\tpayment_minor BIGINT NOT NULL, \n\trefund_minor BIGINT NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tstore_id VARCHAR(80) NOT NULL, \n\tsource_kind VARCHAR(32) NOT NULL, \n\tdata_batch_id VARCHAR(80) NOT NULL, \n\tbatch_generation INTEGER NOT NULL, \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (allocation_id), \n\tUNIQUE (store_id, data_batch_id, batch_generation, order_id, sku_id), \n\tFOREIGN KEY(activity_id) REFERENCES activity_execution (activity_id), \n\tCHECK (status IN ('reserved','consumed','released')), \n\tCHECK (qty>0 AND discount_minor>=0 AND payment_minor>=0 AND refund_minor>=0 AND refund_minor<=payment_minor), \n\tFOREIGN KEY(store_id) REFERENCES store (store_id), \n\tFOREIGN KEY(data_batch_id, batch_generation) REFERENCES source_batch (batch_id, generation), \n\tCHECK (version > 0), \n\tCHECK (source_kind IN ('synthetic','derived','business_import'))\n)\n\n"
    )
    op.execute(
        "CREATE INDEX ix_activity_order ON activity_order_allocation (store_id, data_batch_id, batch_generation, order_id)"
    )
    op.execute(
        'GRANT SELECT, INSERT, UPDATE, DELETE ON "activity_order_allocation" TO cs_agent_app_b0'
    )
    op.execute(
        "\nCREATE TABLE activity_refund_event (\n\tevent_id VARCHAR(80) NOT NULL, \n\trefund_id VARCHAR(80) NOT NULL, \n\tallocation_id VARCHAR(80) NOT NULL, \n\tamount_minor BIGINT NOT NULL, \n\tstore_id VARCHAR(80) NOT NULL, \n\tsource_kind VARCHAR(32) NOT NULL, \n\tdata_batch_id VARCHAR(80) NOT NULL, \n\tbatch_generation INTEGER NOT NULL, \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (event_id), \n\tUNIQUE (refund_id, allocation_id), \n\tFOREIGN KEY(allocation_id) REFERENCES activity_order_allocation (allocation_id), \n\tCHECK (amount_minor>=0), \n\tFOREIGN KEY(store_id) REFERENCES store (store_id), \n\tFOREIGN KEY(data_batch_id, batch_generation) REFERENCES source_batch (batch_id, generation), \n\tCHECK (version > 0), \n\tCHECK (source_kind IN ('synthetic','derived','business_import'))\n)\n\n"
    )
    op.execute('GRANT SELECT, INSERT ON "activity_refund_event" TO cs_agent_app_b0')
    op.execute(
        "\nCREATE TABLE activity_delivery (\n\tevent_id VARCHAR(80) NOT NULL, \n\tsequence BIGINT GENERATED BY DEFAULT AS IDENTITY, \n\tactivity_id VARCHAR(80), \n\tintent_id VARCHAR(80) NOT NULL, \n\tkind VARCHAR(16) NOT NULL, \n\tpayload JSONB NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tavailable_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tlease_token VARCHAR(80), \n\tacked_at TIMESTAMP WITH TIME ZONE, \n\tlast_code VARCHAR(60), \n\tstore_id VARCHAR(80) NOT NULL, \n\tsource_kind VARCHAR(32) NOT NULL, \n\tdata_batch_id VARCHAR(80) NOT NULL, \n\tbatch_generation INTEGER NOT NULL, \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (event_id), \n\tCHECK (kind IN ('receipt','actuals') AND status IN ('pending','leased','acked') AND attempts>=0), \n\tFOREIGN KEY(store_id) REFERENCES store (store_id), \n\tFOREIGN KEY(data_batch_id, batch_generation) REFERENCES source_batch (batch_id, generation), \n\tCHECK (version > 0), \n\tCHECK (source_kind IN ('synthetic','derived','business_import'))\n)\n\n"
    )
    op.execute(
        "CREATE INDEX ix_activity_delivery_intent ON activity_delivery (intent_id, sequence)"
    )
    op.execute(
        "CREATE INDEX ix_activity_delivery_ready ON activity_delivery (status, available_at, sequence)"
    )
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON "activity_delivery" TO cs_agent_app_b0')
    op.execute("GRANT USAGE, SELECT ON SEQUENCE activity_delivery_sequence_seq TO cs_agent_app_b0")


def downgrade():
    raise RuntimeError("活动与营销账本不可自动回退")
