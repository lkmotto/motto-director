import asyncio
import argparse
import logging
import os
import signal

from dotenv import load_dotenv
load_dotenv()

from director.perceive import perceive
from director.ideate import ideate
from director.act import act
from director.fleet_client import FleetClient
from director.factory_client import FactoryClient
from director.session_store import SessionStore
from director.goals import GoalStore

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
log = logging.getLogger('motto-director')

CYCLE_INTERVAL = int(os.getenv('CYCLE_INTERVAL_SECONDS', '120'))
MAX_PARALLEL_DROIDS = int(os.getenv('MAX_PARALLEL_DROIDS', '5'))

_stop = False


def _handle_signal(sig, frame):
    global _stop
    log.info('Received signal %s, stopping after current cycle...', sig)
    _stop = True


async def run_cycle(fleet: FleetClient, factory: FactoryClient,
                    sessions: SessionStore, goals: GoalStore):
    run_id = None
    try:
        run_id = await fleet.record_run_start(
            'motto-director', 'perceive_ideate_act_cycle', 'autonomous'
        )
    except Exception as exc:
        log.warning('Could not record run start: %s', exc)

    try:
        log.info('--- PERCEIVE ---')
        perception = await perceive(fleet, sessions, goals)
        log.info('Goals=%d sessions=%d events=%d intents=%d',
                 len(perception.active_goals), len(perception.active_sessions),
                 len(perception.recent_events), len(perception.open_intents))

        log.info('--- IDEATE ---')
        tasks = await ideate(perception, max_droids=MAX_PARALLEL_DROIDS)
        log.info('Ideated %d tasks', len(tasks))
        for t in tasks:
            log.info('  task: goal=%s repo=%s title=%s', t.get('goal_id'), t.get('repo'), t.get('task_title'))

        log.info('--- ACT ---')
        await act(tasks, fleet, factory, sessions, goals)

        if run_id:
            try:
                await fleet.record_run_end(run_id, 'success', {'tasks_spawned': len(tasks)})
            except Exception as exc:
                log.warning('record_run_end failed: %s', exc)

    except Exception as exc:
        log.exception('Cycle error: %s', exc)
        if run_id:
            try:
                await fleet.record_run_end(run_id, 'error', {'error': str(exc)})
            except Exception:
                pass
        raise


async def main(once: bool = False):
    global _stop

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    fleet = FleetClient()
    factory = FactoryClient()
    sessions = SessionStore()
    goals = GoalStore()

    log.info('motto-director starting (once=%s interval=%ds)', once, CYCLE_INTERVAL)

    cycle = 0
    while not _stop:
        cycle += 1
        log.info('=== CYCLE %d ===', cycle)
        try:
            await run_cycle(fleet, factory, sessions, goals)
        except Exception:
            log.error('Cycle %d failed, continuing...', cycle)

        if once or _stop:
            break

        log.info('Sleeping %ds until next cycle...', CYCLE_INTERVAL)
        await asyncio.sleep(CYCLE_INTERVAL)

    log.info('motto-director stopped after %d cycles', cycle)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='motto-director autonomous loop')
    parser.add_argument('--once', action='store_true', help='Run one cycle and exit')
    args = parser.parse_args()
    asyncio.run(main(once=args.once))
