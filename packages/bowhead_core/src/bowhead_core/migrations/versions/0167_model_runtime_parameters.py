"""Version optional model request policies in the encrypted Team config.

Advance all ledgers without DDL or rewriting existing ciphertext. Absence of
runtime_parameters retains legacy request semantics and evidence signatures.
"""

revision = "0167_model_runtime_parameters"
down_revision = "0166_shared_agent_personal_mcp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass
