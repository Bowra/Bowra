"""Retain source-bound document routing inside ordinary Lead loop v4.

This is a new encrypted-envelope format, not a table, permission or job.
V1/v2/v3 bytes and digest interpretation stay unchanged. All eight target
ledgers advance without DDL or historical rewriting; older readers cannot
consume newly captured routing-aware checkpoints.
"""

revision = "0155_document_routing"
down_revision = "0154_ordinary_material_identity"
branch_labels = None
depends_on = None


def upgrade():
    pass
