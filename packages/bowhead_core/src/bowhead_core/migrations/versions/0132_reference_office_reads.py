"""Version the retained Office snapshot and read-question envelopes.

The existing encrypted delegation can now contain attachment snapshot v2;
the existing encrypted question can contain transcript v5. These formats add
deferred upload identities and successful attachment_read calls respectively.
Historical ciphertext remains byte-identical. No relational DDL or backfill
is required; the revision prevents rollback to readers of the older formats.
"""

revision = "0132_reference_office_reads"
down_revision = "0131_document_recovery"
branch_labels = None
depends_on = None


def upgrade():
    pass
