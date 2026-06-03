"""Seed sample agents and tickets for local development.

Run via: make seed   or   python -m scripts.seed

``--agents-only`` seeds support agents only (used by ``make demo-api`` so worker
events on demo tickets always come from the live queue, not static seed rows).

Idempotent: skips rows that already exist (agents by email, tickets by email+subject).
Safe to re-run after ``make migrate`` or on a fresh database.

Full seed does not enqueue arq jobs — audit events on sample tickets are written
directly so ``GET /tickets/{id}`` is useful immediately without waiting for the worker.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select

from app.database import async_session_factory
from app.enums import Category, EventType, Priority, TicketStatus
from app.models import Agent, Ticket, TicketEvent

DEFAULT_AGENTS: tuple[tuple[str, str], ...] = (
    ("Ada Support", "ada@support.example.com"),
    ("Grace Handler", "grace@support.example.com"),
    ("Lin Ops", "lin@support.example.com"),
)


@dataclass(frozen=True)
class SeedTicketSpec:
    """One deterministic sample ticket."""

    customer_name: str
    customer_email: str
    subject: str
    description: str
    priority: Priority
    category: Category
    status: TicketStatus
    assign_to_email: str | None = None
    demo_events: tuple[TicketEvent, ...] = ()

    def identity_key(self) -> tuple[str, str]:
        return (self.customer_email, self.subject)


def _demo_event(
    event_type: EventType,
    field_changed: str,
    new_value: str,
    *,
    previous_value: str | None = None,
) -> TicketEvent:
    """Build a TicketEvent template (ticket_id filled in at insert time)."""
    return TicketEvent(
        ticket_id=0,
        event_type=event_type,
        field_changed=field_changed,
        previous_value=previous_value,
        new_value=new_value,
    )


DEFAULT_TICKETS: tuple[SeedTicketSpec, ...] = (
    SeedTicketSpec(
        customer_name="Ada Lovelace",
        customer_email="ada@example.com",
        subject="Cannot reset my password",
        description=(
            "The password reset link returns a 404 after I click it from the email. "
            "I tried twice on Chrome and once on Firefox."
        ),
        priority=Priority.MEDIUM,
        category=Category.TECHNICAL,
        status=TicketStatus.OPEN,
    ),
    SeedTicketSpec(
        customer_name="Charles Babbage",
        customer_email="charles@example.com",
        subject="Production API outage — urgent",
        description=(
            "Our production API has been down for twenty minutes. Customers cannot "
            "check out. This is a critical emergency affecting all regions."
        ),
        priority=Priority.HIGH,
        category=Category.TECHNICAL,
        status=TicketStatus.IN_PROGRESS,
        assign_to_email="ada@support.example.com",
        demo_events=(
            _demo_event(
                EventType.ROUTED,
                "department",
                "technical-support",
            ),
            _demo_event(
                EventType.PRIORITY_CHANGED,
                "priority",
                Priority.CRITICAL.value,
                previous_value=Priority.HIGH.value,
            ),
            _demo_event(
                EventType.SUMMARIZED,
                "summary",
                "Production API outage blocking customer checkout across all regions.",
            ),
        ),
    ),
    SeedTicketSpec(
        customer_name="Grace Hopper",
        customer_email="grace@example.com",
        subject="Charged twice for March invoice",
        description=(
            "I see two identical charges of $49.99 on my card for the March billing "
            "cycle. Please refund the duplicate."
        ),
        priority=Priority.LOW,
        category=Category.BILLING,
        status=TicketStatus.RESOLVED,
        assign_to_email="grace@support.example.com",
    ),
    SeedTicketSpec(
        customer_name="Alan Turing",
        customer_email="alan@example.com",
        subject="Add dark mode to the dashboard",
        description=(
            "The dashboard is hard to use at night. A dark theme would help long "
            "support shifts. Happy to beta test."
        ),
        priority=Priority.MEDIUM,
        category=Category.FEATURE_REQUEST,
        status=TicketStatus.CLOSED,
        assign_to_email="lin@support.example.com",
    ),
    SeedTicketSpec(
        customer_name="Spam Sender",
        customer_email="spam@bad.example.com",
        subject="You have won a free offer!!!",
        description=(
            "Click here now for a limited time congratulations prize. "
            "Visit https://evil.example/a and https://evil.example/b and "
            "https://evil.example/c to claim."
        ),
        priority=Priority.MEDIUM,
        category=Category.OTHER,
        status=TicketStatus.OPEN,
        demo_events=(
            _demo_event(EventType.SPAM_FLAGGED, "spam", "true"),
            _demo_event(EventType.ROUTED, "department", "general"),
        ),
    ),
)

_STATUS_TRANSITIONS: dict[TicketStatus, Sequence[tuple[TicketStatus, TicketStatus]]] = {
    TicketStatus.OPEN: (),
    TicketStatus.IN_PROGRESS: ((TicketStatus.OPEN, TicketStatus.IN_PROGRESS),),
    TicketStatus.RESOLVED: (
        (TicketStatus.OPEN, TicketStatus.IN_PROGRESS),
        (TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED),
    ),
    TicketStatus.CLOSED: ((TicketStatus.OPEN, TicketStatus.CLOSED),),
}


def _status_events(
    ticket_id: int,
    status: TicketStatus,
    *,
    actor_id: int | None,
) -> list[TicketEvent]:
    events = [
        TicketEvent(
            ticket_id=ticket_id,
            event_type=EventType.CREATED,
            new_value=TicketStatus.OPEN.value,
        )
    ]
    for previous, new in _STATUS_TRANSITIONS[status]:
        events.append(
            TicketEvent(
                ticket_id=ticket_id,
                event_type=EventType.STATUS_CHANGED,
                field_changed="status",
                previous_value=previous.value,
                new_value=new.value,
                actor_id=actor_id,
            )
        )
    return events


async def seed_agents() -> int:
    """Insert default agents; return count of newly created rows."""
    created = 0
    async with async_session_factory() as session:
        for name, email in DEFAULT_AGENTS:
            existing = await session.scalar(
                select(Agent.id).where(Agent.email == email)
            )
            if existing is not None:
                print(f"skip agent {email} (already exists)")
                continue
            session.add(Agent(name=name, email=email))
            created += 1
            print(f"add  agent {email} ({name})")
        await session.commit()
    return created


async def seed_tickets() -> int:
    """Insert default tickets; return count of newly created rows."""
    created = 0
    async with async_session_factory() as session:
        agent_ids = {
            email: agent_id
            for email, agent_id in (
                await session.execute(select(Agent.email, Agent.id))
            ).all()
        }

        for spec in DEFAULT_TICKETS:
            existing = await session.scalar(
                select(Ticket.id).where(
                    Ticket.customer_email == spec.customer_email,
                    Ticket.subject == spec.subject,
                )
            )
            if existing is not None:
                print(f"skip ticket {spec.subject!r} (already exists)")
                continue

            assign_id = None
            if spec.assign_to_email is not None:
                assign_id = agent_ids.get(spec.assign_to_email)
                if assign_id is None:
                    print(
                        f"warn ticket {spec.subject!r}: agent "
                        f"{spec.assign_to_email} missing, leaving unassigned",
                        file=sys.stderr,
                    )

            ticket = Ticket(
                customer_name=spec.customer_name,
                customer_email=spec.customer_email,
                subject=spec.subject,
                description=spec.description,
                priority=spec.priority,
                category=spec.category,
                status=spec.status,
                assigned_agent_id=assign_id,
            )
            session.add(ticket)
            await session.flush()

            actor_id = assign_id
            for event in _status_events(ticket.id, spec.status, actor_id=actor_id):
                session.add(event)
            for template in spec.demo_events:
                session.add(
                    TicketEvent(
                        ticket_id=ticket.id,
                        event_type=template.event_type,
                        field_changed=template.field_changed,
                        previous_value=template.previous_value,
                        new_value=template.new_value,
                    )
                )

            created += 1
            print(f"add  ticket #{ticket.id} {spec.status.value} — {spec.subject!r}")

        await session.commit()
    return created


async def run_seed(*, agents_only: bool = False) -> tuple[int, int]:
    """Seed agents and optionally sample tickets."""
    agents_created = await seed_agents()
    if agents_only:
        return agents_created, 0
    tickets_created = await seed_tickets()
    return agents_created, tickets_created


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed agents and sample tickets.")
    parser.add_argument(
        "--agents-only",
        action="store_true",
        help="seed agents only; skip sample tickets (for make demo-api)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    try:
        agents_created, tickets_created = asyncio.run(
            run_seed(agents_only=args.agents_only)
        )
    except Exception as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if args.agents_only:
        print(
            f"done ({agents_created} agent(s) created, tickets skipped — agents-only)"
        )
    else:
        print(f"done ({agents_created} agent(s), {tickets_created} ticket(s) created)")


if __name__ == "__main__":
    main()
