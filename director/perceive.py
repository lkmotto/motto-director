import asyncio
from dataclasses import dataclass, field

from .fleet_client import FleetClient
from .session_store import SessionStore
from .goals import GoalStore


@dataclass
class PerceptionBundle:
    fleet_agents: list[dict] = field(default_factory=list)
    recent_events: list[dict] = field(default_factory=list)
    open_intents: list[dict] = field(default_factory=list)
    active_sessions: dict = field(default_factory=dict)
    active_goals: list[dict] = field(default_factory=list)


async def perceive(fleet: FleetClient, session_store: SessionStore, goals: GoalStore) -> PerceptionBundle:
    fleet_status_task = asyncio.create_task(fleet.get_fleet_status())
    recent_events_task = asyncio.create_task(fleet.get_recent_events(since_minutes=60))
    open_intents_task = asyncio.create_task(fleet.consume_open_intents('motto-director'))

    fleet_agents, recent_events, open_intents = await asyncio.gather(
        fleet_status_task,
        recent_events_task,
        open_intents_task,
        return_exceptions=True,
    )

    # Degrade gracefully on partial failures
    if isinstance(fleet_agents, Exception):
        fleet_agents = []
    if isinstance(recent_events, Exception):
        recent_events = []
    if isinstance(open_intents, Exception):
        open_intents = []

    return PerceptionBundle(
        fleet_agents=fleet_agents,
        recent_events=recent_events,
        open_intents=open_intents,
        active_sessions=session_store.get_all(),
        active_goals=goals.get_active_goals(),
    )
