from __future__ import annotations

from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from bowhead_core.sqlite_domain_runtime import DomainConnection, guard_connection
from bowhead_core.sqlite_domains import DOMAIN_LAYOUT, DOMAIN_TARGET_FILES
from bowhead_core.sqlite_upgrade import _load_state

config = context.config


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"factory": DomainConnection, "uri": True},
        hide_parameters=True,
    )
    with connectable.connect() as connection:
        raw_connection = connection.connection.driver_connection
        database = next(row[2] for row in raw_connection.execute("PRAGMA database_list") if row[1] == "main")
        if database:
            directory = Path(database).parent
            state = _load_state(directory)
            if state is not None and state["layout"] == DOMAIN_LAYOUT:
                if state["status"] != "ready" and not config.attributes.get("bowhead_domain_coordinator"):
                    raise RuntimeError("Domain migrations require ready layout admission or the verified offline cleanup barrier")
                guard_connection(raw_connection, directory / DOMAIN_TARGET_FILES["core"], config.attributes["bowhead_target"])
        raw_connection.execute("PRAGMA main.synchronous=FULL")
        if raw_connection.execute("PRAGMA main.synchronous").fetchone()[0] != 2:
            raise RuntimeError("SQLite migrations require synchronous=FULL")
        context.configure(connection=connection, transaction_per_migration=False)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
