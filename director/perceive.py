import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from .fleet_client import FleetClient
from .session_store import SessionStore
from .goals import GoalStore

log = logging.getLogger(__name__)

DEFAULT_INTENT_LIMIT = 10


@dataclass
class PerceptionBundle:
    fleet_agents: list = field(default_factory=list)
    recent_events: list = field(default_factory=list)
    open_intents: list = field(default_factory=list)
    active_sessions: list = field(default_factory=list)
    active_goals: list = field(default_factory=list)

    @property
    def perceived_intents(self) -> list:
        return self.open_intents


async def perceive(
    fleet: FleetClient,
    session_store: SessionStore,
    goals: Optional[GoalStore] = None,
    intent_limit: int = DEFAULT_INTENT_LIMIT,
) -> PerceptionBundle:
    fleet_status_task = asyncio.create_task(fleet.get_fleet_status())
    recent_events_task = asyncio.create_task(fleet.get_recent_events(since_minutes=60))
    open_intents_task = asyncio.create_task(
        fleet.consume_open_intents('motto-director', limit=intent_limit)
    )

    fleet_agents, recent_events, open_intents = await asyncio.gather(
        fleet_status_task,
        recent_events_task,
        open_intents_task,
        return_exceptions=True,
    )

    if isinstance(fleet_agents, Exception):
        log.warning('get_fleet_status failed: %s', fleet_agents)
        fleet_agents = []
    if isinstance(recent_events, Exception):
        log.warning('get_recent_events failed: %s', recent_events)
        recent_events = []
    if isinstance(open_intents, Exception):
        log.warning('consume_open_intents failed: %s', open_intents)
        open_intents = []

    log.info('Consumed %d intent(s) for motto-director', len(open_intents))

    return PerceptionBundle(
        fleet_agents=fleet_agents,
        recent_events=recent_events,
        open_intents=open_intents,
        active_sessions=session_store.get_all(),
        active_goals=goals.get_active_goals() if goals else [],
    )
