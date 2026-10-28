"""Version ordered multi-helper material inside the existing encrypted card.

All eight ledgers advance without DDL or historical rewrites. V1-v4 retain
their original bytes and readers; v5 requires the ordered helper reader.
"""

revision = "0160_ordinary_multi_helper"
down_revision = "0159_release_plugin_versions"
branch_labels = None
depends_on = None


def upgrade():
    pass
