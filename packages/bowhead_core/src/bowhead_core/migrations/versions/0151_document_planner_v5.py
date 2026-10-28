"""Version retained document planner contexts and runtime snapshots.

New Turn-owned admissions can retain pptx-grounded-plan-v5. Older v4 prompts,
contexts, receipts and controls remain byte-identical. The profile is inside
existing JSON envelopes, so no relational DDL or historical backfill is needed.
All target ledgers advance to record the older-reader compatibility boundary.
"""

revision = "0151_document_planner_v5"
down_revision = "0150_document_clarifications"
branch_labels = None
depends_on = None


def upgrade():
    pass
