"""Version ordinary v15 and typed attachment uses without changing domain rows."""

revision = "0227_observed_continuation_inputs"
down_revision = "0226_notification_deliveries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing encrypted records retain their original readers and exact bytes.
    pass
