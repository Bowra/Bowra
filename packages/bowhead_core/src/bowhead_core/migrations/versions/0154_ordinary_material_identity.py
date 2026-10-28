"""Version ordinary retained artifact identity without rewriting old proofs.

The v3 loop envelope excludes asynchronous classification labels from artifact
digests. Existing v1/v2 encrypted checkpoints retain their original digest
semantics. All targets advance; no relational schema or historical rows change.
"""

revision = "0154_ordinary_material_identity"
down_revision = "0153_memory_turn_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass
