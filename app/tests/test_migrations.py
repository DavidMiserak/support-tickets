"""Exercise the Alembic migrations end to end.

conftest builds the schema from ``Base.metadata`` for normal tests, so the
migrations themselves are otherwise never run. This test applies them to a real
database and rolls them back, catching breakage the metadata path can't (column
adds, the event_type CHECK, FK ON DELETE rules, data backfills).

Migrations run via the alembic CLI (subprocess), the same way they run in
production — and the way that avoids the local ``alembic/`` package shadowing
the installed library.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest

from app.tests.conftest import TEST_DATABASE_URL

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_BIN = Path(sys.executable).parent / "alembic"
_DSN = TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


def _run(coro):
    return asyncio.run(coro)


def _alembic(*args: str) -> None:
    """Invoke the alembic CLI against the test database."""
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL}
    result = subprocess.run(
        [str(ALEMBIC_BIN), *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
        )


def _db_reachable() -> bool:
    async def _probe() -> None:
        conn = await asyncpg.connect(_DSN)
        await conn.close()

    try:
        _run(_probe())
        return True
    except Exception:
        return False


async def _reset_database() -> None:
    """Drop everything the migrations create, plus Alembic's version table."""
    conn = await asyncpg.connect(_DSN)
    try:
        for table in ("ticket_events", "tickets", "agents", "alembic_version"):
            await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        # Native enum types from 0001 must go too, or re-upgrade fails.
        for enum_type in ("ticketstatus", "priority", "category"):
            await conn.execute(f"DROP TYPE IF EXISTS {enum_type} CASCADE")
    finally:
        await conn.close()


async def _column_exists(table: str, column: str) -> bool:
    conn = await asyncpg.connect(_DSN)
    try:
        return (
            await conn.fetchval(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = $1 AND column_name = $2",
                table,
                column,
            )
        ) is not None
    finally:
        await conn.close()


async def _table_exists(table: str) -> bool:
    conn = await asyncpg.connect(_DSN)
    try:
        return (await conn.fetchval("SELECT to_regclass($1)", table)) is not None
    finally:
        await conn.close()


async def _assert_schema_semantics() -> None:
    """Assert the migrated schema enforces what 0002 set up.

    Runs against the migration-built schema (not Base.metadata.create_all), so a
    wrong CHECK value list or a missing ON DELETE rule in the migration is caught.
    """
    conn = await asyncpg.connect(_DSN)
    try:
        agent_id = await conn.fetchval(
            "INSERT INTO agents (name, email) VALUES ($1, $2) RETURNING id",
            "Mig Agent",
            "mig@example.com",
        )
        ticket_id = await conn.fetchval(
            "INSERT INTO tickets (customer_name, customer_email, subject, "
            "description, category, assigned_agent_id) "
            "VALUES ($1, $2, $3, $4, $5::category, $6) RETURNING id",
            "Cust",
            "cust@example.com",
            "Subj",
            "Desc",
            "OTHER",
            agent_id,
        )

        # event_type CHECK rejects an out-of-set value...
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                "INSERT INTO ticket_events (ticket_id, event_type) VALUES ($1, $2)",
                ticket_id,
                "DELETED",
            )
        # ...and accepts a valid one.
        await conn.execute(
            "INSERT INTO ticket_events (ticket_id, event_type) VALUES ($1, $2)",
            ticket_id,
            "CREATED",
        )

        # ON DELETE SET NULL: deleting the agent nulls the assignment.
        await conn.execute("DELETE FROM agents WHERE id = $1", agent_id)
        assigned = await conn.fetchval(
            "SELECT assigned_agent_id FROM tickets WHERE id = $1", ticket_id
        )
        assert assigned is None, "assigned_agent_id was not set NULL on agent delete"
    finally:
        await conn.close()


async def _assert_0004_semantics(ticket_id: int) -> None:
    """Assert migration 0004 extended the CHECK to accept SUMMARIZED and ASSIGNED."""
    conn = await asyncpg.connect(_DSN)
    try:
        # Both new values must be accepted.
        for ev in ("SUMMARIZED", "ASSIGNED"):
            await conn.execute(
                "INSERT INTO ticket_events (ticket_id, event_type) VALUES ($1, $2)",
                ticket_id,
                ev,
            )
        # A value outside the extended set must still be rejected.
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                "INSERT INTO ticket_events (ticket_id, event_type) VALUES ($1, $2)",
                ticket_id,
                "DELETED",
            )
    finally:
        await conn.close()


@pytest.mark.no_auto_schema
def test_migrations_upgrade_then_downgrade() -> None:
    """`upgrade head` then `downgrade base` both succeed on a clean database."""
    if not _db_reachable():
        pytest.skip("no test database reachable")

    _run(_reset_database())
    try:
        _alembic("upgrade", "head")

        # 0002 added these to ticket_events.
        assert _run(_column_exists("ticket_events", "actor_id"))
        assert _run(_column_exists("ticket_events", "field_changed"))
        assert _run(_table_exists("tickets"))

        # The migration's CHECK + ON DELETE SET NULL actually enforce (0002 semantics).
        _run(_assert_schema_semantics())

        # Seed a ticket_id visible to the 0004 assertion helper.
        async def _seed_ticket_id() -> int:
            conn = await asyncpg.connect(_DSN)
            try:
                row_id = await conn.fetchval(
                    "INSERT INTO tickets (customer_name, customer_email, subject, "
                    "description, category) VALUES ($1,$2,$3,$4,$5::category) RETURNING id",
                    "T",
                    "t@t.com",
                    "S",
                    "D",
                    "OTHER",
                )
                return int(row_id)
            finally:
                await conn.close()

        tid = _run(_seed_ticket_id())

        # 0004 extended the CHECK to include SUMMARIZED and ASSIGNED.
        _run(_assert_0004_semantics(tid))

        _alembic("downgrade", "base")

        # base drops every table the migrations created.
        assert not _run(_table_exists("ticket_events"))
        assert not _run(_table_exists("tickets"))
        assert not _run(_table_exists("agents"))
    finally:
        _run(_reset_database())
