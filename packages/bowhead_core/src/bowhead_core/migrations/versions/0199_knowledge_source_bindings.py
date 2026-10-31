"""Version Agent source policies and effective scopes in retained execution material.

Definition knowledge_access v1, ordinary v13 and helper material v3 are new
contracts. Historical definitions and checkpoints remain byte-identical.
"""

revision = "0199_knowledge_source_bindings"
down_revision = "0198_artifact_presentation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError("Restore the paired database snapshot to roll back.")
