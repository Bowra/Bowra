"""New continuation proof formats preserve every existing domain row and schema."""

import pytest
from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES
from bowhead_core.sqlite_upgrade import database_head
from test_context_material_recovery_migration import advance_to
from test_sqlite_domain_upgrade import directory as directory
from test_sqlite_domain_upgrade import legacy_source as legacy_source
from test_sqlite_mutation_migration import evidence

pytestmark = pytest.mark.integration


def test_observed_input_format_upgrade_preserves_all_domains_and_is_idempotent(directory):
    previous = "0226_notification_deliveries"
    current = "0227_observed_continuation_inputs"
    advance_to(directory, previous)
    before = evidence(directory)
    advance_to(directory, current)
    assert evidence(directory) == before
    assert all(database_head(directory / name) == current for name in DOMAIN_TARGET_FILES.values())
    advance_to(directory, current)
    assert evidence(directory) == before
