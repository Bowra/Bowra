"""Keep v2 model credit evidence beside the existing credit ledger."""

from alembic import context

revision = "0173_model_credit_rates"
down_revision = "0172_team_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    target = context.config.attributes["bowhead_target"]
    if target not in {"core", "matrix"}:
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT model_credit_rates")
    try:
        if target == "matrix":
            db.exec_driver_sql("ALTER TABLE kb_matrix_runs ADD COLUMN billing_rate_card_version TEXT CHECK(billing_rate_card_version IS NULL OR billing_rate_card_version IN ('v1','v2'))")
            db.exec_driver_sql("""
                CREATE TRIGGER matrix_credit_rate_at_insert AFTER INSERT ON kb_matrix_runs
                WHEN NEW.billing_rate_card_version IS NULL
                BEGIN UPDATE kb_matrix_runs SET billing_rate_card_version='v2' WHERE run_id=NEW.run_id; END
            """)
            db.exec_driver_sql("RELEASE SAVEPOINT model_credit_rates")
            return
        db.exec_driver_sql("ALTER TABLE turns ADD COLUMN billing_rate_card_version TEXT CHECK(billing_rate_card_version IS NULL OR billing_rate_card_version IN ('v1','v2'))")
        db.exec_driver_sql("""
            CREATE TRIGGER turn_credit_rate_at_insert AFTER INSERT ON turns
            WHEN NEW.billing_rate_card_version IS NULL
            BEGIN UPDATE turns SET billing_rate_card_version='v2' WHERE id=NEW.id; END
        """)
        db.exec_driver_sql("ALTER TABLE turn_generations ADD COLUMN billing_usage_json TEXT CHECK(billing_usage_json IS NULL OR json_valid(billing_usage_json))")
        db.exec_driver_sql("ALTER TABLE team_subagent_draft_usage ADD COLUMN billing_rate_card_version TEXT CHECK(billing_rate_card_version IS NULL OR billing_rate_card_version IN ('v1','v2'))")
        db.exec_driver_sql("ALTER TABLE team_subagent_draft_usage ADD COLUMN model_usage_json TEXT CHECK(model_usage_json IS NULL OR json_valid(model_usage_json))")
        db.exec_driver_sql("""
            CREATE TRIGGER draft_credit_rate_at_insert AFTER INSERT ON team_subagent_draft_usage
            WHEN NEW.billing_rate_card_version IS NULL
            BEGIN UPDATE team_subagent_draft_usage SET billing_rate_card_version='v2'
              WHERE team_id=NEW.team_id AND owner_user_id=NEW.owner_user_id AND request_key_hash=NEW.request_key_hash; END
        """)
        db.exec_driver_sql("ALTER TABLE document_usage_receipts ADD COLUMN credit_rates_json TEXT CHECK(credit_rates_json IS NULL OR json_valid(credit_rates_json))")
        db.exec_driver_sql("ALTER TABLE document_usage_receipts ADD COLUMN usage_family TEXT")
        db.exec_driver_sql("ALTER TABLE document_model_calls ADD COLUMN credit_rates_json TEXT CHECK(credit_rates_json IS NULL OR json_valid(credit_rates_json))")
        db.exec_driver_sql("ALTER TABLE document_model_calls ADD COLUMN usage_family TEXT")
        db.exec_driver_sql("""
            CREATE TABLE credit_model_usage (
                ledger_id TEXT NOT NULL REFERENCES credit_ledger(id) ON DELETE CASCADE,
                line_no INTEGER NOT NULL CHECK(line_no >= 1),
                provider TEXT,
                model TEXT,
                tokens_input INTEGER NOT NULL CHECK(tokens_input >= 0),
                tokens_cached INTEGER NOT NULL CHECK(tokens_cached >= 0),
                tokens_output INTEGER NOT NULL CHECK(tokens_output >= 0),
                rate_input TEXT NOT NULL,
                rate_cached TEXT NOT NULL,
                rate_output TEXT NOT NULL,
                weighted_tokens TEXT NOT NULL,
                PRIMARY KEY (ledger_id, line_no)
            )
        """)
        db.exec_driver_sql("CREATE INDEX idx_credit_model_usage_model ON credit_model_usage(provider, model)")
        db.exec_driver_sql("""
            CREATE TRIGGER credit_model_usage_parent_delete AFTER DELETE ON credit_ledger
            BEGIN DELETE FROM credit_model_usage WHERE ledger_id=OLD.id; END
        """)
        db.exec_driver_sql("""
            CREATE TRIGGER credit_model_usage_no_update BEFORE UPDATE ON credit_model_usage
            BEGIN SELECT RAISE(ABORT, 'credit model usage is immutable'); END
        """)
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT model_credit_rates")
        db.exec_driver_sql("RELEASE SAVEPOINT model_credit_rates")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT model_credit_rates")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
