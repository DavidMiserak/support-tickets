"""Seed sample support agents for local development.

Run via: make seed   or   python -m scripts.seed

Idempotent: skips agents that already exist (matched by email). Safe to re-run
after ``make migrate`` or on a fresh database.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.database import async_session_factory
from app.models import Agent

DEFAULT_AGENTS: tuple[tuple[str, str], ...] = (
    ("Ada Support", "ada@support.example.com"),
    ("Grace Handler", "grace@support.example.com"),
    ("Lin Ops", "lin@support.example.com"),
)


async def seed_agents() -> int:
    """Insert default agents; return count of newly created rows."""
    created = 0
    async with async_session_factory() as session:
        for name, email in DEFAULT_AGENTS:
            existing = await session.scalar(
                select(Agent.id).where(Agent.email == email)
            )
            if existing is not None:
                print(f"skip {email} (already exists)")
                continue
            session.add(Agent(name=name, email=email))
            created += 1
            print(f"add  {email} ({name})")
        await session.commit()
    return created


def main() -> None:
    try:
        created = asyncio.run(seed_agents())
    except Exception as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(f"done ({created} agent(s) created)")


if __name__ == "__main__":
    main()
