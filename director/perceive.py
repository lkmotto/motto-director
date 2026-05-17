import asyncio
from dataclasses import dataclass, field
from typing import List

from .fleet_client import FleetClient
from .session_store import SessionStore, SessionMeta
from .goals import GoalStore


@dataclass
class PerceptionBundle:
    fleet_agents: list = field(default_factory=list)
    recent_events: list = field(default_factory=list)
    open_intents: list = field(default_factory=list)
    active_sessions: list = field(default_factory=list)  # list[SessionMeta]
    active_goals: list = field(default_factory=list)


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
