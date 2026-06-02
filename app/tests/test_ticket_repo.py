"""Repository tests against a real database."""

import pytest

from app.enums import Category, Priority, TicketStatus
from app.models import Ticket
from app.repositories.ticket import TicketRepository


def _ticket(**overrides: object) -> Ticket:
    data: dict[str, object] = {
        "customer_name": "Cust",
        "customer_email": "cust@example.com",
        "subject": "Subj",
        "description": "Desc",
        "category": Category.OTHER,
    }
    data.update(overrides)
    return Ticket(**data)


@pytest.mark.asyncio
async def test_add_flushes_id_and_get_round_trips(test_db):
    repo = TicketRepository(test_db)
    ticket = await repo.add(_ticket())
    assert ticket.id is not None  # flushed without commit
    await test_db.commit()

    found = await repo.get(ticket.id)
    assert found is not None
    assert found.subject == "Subj"


@pytest.mark.asyncio
async def test_get_missing_returns_none(test_db):
    repo = TicketRepository(test_db)
    assert await repo.get(999_999) is None


@pytest.mark.asyncio
async def test_list_filters_and_total(test_db):
    repo = TicketRepository(test_db)
    for _ in range(3):
        await repo.add(_ticket(status=TicketStatus.OPEN))
    for _ in range(2):
        await repo.add(_ticket(status=TicketStatus.CLOSED, priority=Priority.HIGH))
    await test_db.commit()

    items, total = await repo.list(status=TicketStatus.OPEN)
    assert total == 3
    assert all(t.status == TicketStatus.OPEN for t in items)

    items, total = await repo.list(status=TicketStatus.CLOSED, priority=Priority.HIGH)
    assert total == 2


@pytest.mark.asyncio
async def test_list_pagination_is_stable_across_pages(test_db):
    repo = TicketRepository(test_db)
    for i in range(5):
        await repo.add(_ticket(subject=f"S{i}"))
    await test_db.commit()

    # The 5 rows share a created_at (same transaction's now()), so a stable
    # order needs the id tiebreaker. Page through and assert no row is skipped
    # or duplicated, and the order is deterministic (id desc).
    seen_ids: list[int] = []
    for skip in (0, 2, 4):
        page, total = await repo.list(skip=skip, limit=2)
        assert total == 5
        seen_ids.extend(t.id for t in page)

    assert len(seen_ids) == 5
    assert len(set(seen_ids)) == 5  # no duplicates across pages
    assert seen_ids == sorted(seen_ids, reverse=True)  # stable, newest-first

    past_end, total = await repo.list(skip=100, limit=10)
    assert past_end == []
    assert total == 5
