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
                    sessions: SessionStore, goals: GoalStore | None,
                    self_directed: bool = False) -> bool:
    run_id = None
    try:
        run_id = await fleet.record_run_start(
            'motto-director',
            'perceive_ideate_act_cycle',
            'intent-driven',
        )
    except Exception as exc:
        log.warning('Could not record run start: %s', exc)

    try:
        log.info('--- PERCEIVE ---')
        perception = await perceive(fleet, sessions, goals)
        log.info('sessions=%d events=%d intents=%d',
                 len(perception.active_sessions),
                 len(perception.recent_events), len(perception.open_intents))

        tasks: list[dict] = []
        intents = perception.perceived_intents
        if intents:
            log.info('--- IDEATE ---')
            for intent in intents:
                generated = await ideate(
                    perception,
                    intent=intent,
                    max_droids=MAX_PARALLEL_DROIDS,
                    self_directed=self_directed,
                )
                tasks.extend(generated)
            log.info('Ideated %d task(s) from %d intent(s)', len(tasks), len(intents))
        elif self_directed:
            generated = await ideate(
                perception,
                intent=None,
                max_droids=MAX_PARALLEL_DROIDS,
                self_directed=True,
            )
            tasks.extend(generated)
            log.info('Self-directed fallback generated %d task(s)', len(tasks))
        else:
            log.info('No pending intents. Exiting.')
            if run_id:
                try:
                    await fleet.record_run_end(run_id, 'success', {'tasks_spawned': 0, 'intents': 0})
                except Exception as exc:
                    log.warning('record_run_end failed: %s', exc)
            return False

        log.info('--- ACT ---')
        await act(tasks, fleet, factory, sessions, goals)

        if run_id:
            try:
                await fleet.record_run_end(
                    run_id,
                    'success',
                    {'tasks_spawned': len(tasks), 'intents': len(intents)},
                )
            except Exception as exc:
                log.warning('record_run_end failed: %s', exc)
        return True

    except Exception as exc:
        log.exception('Cycle error: %s', exc)
        if run_id:
            try:
                await fleet.record_run_end(run_id, 'error', {'error': str(exc)})
            except Exception:
                pass
        raise


async def main(once: bool = False, self_directed: bool = False):
    global _stop

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    fleet = FleetClient()
    factory = FactoryClient()
    sessions = SessionStore()
    goals = GoalStore() if self_directed else None

    log.info(
        'motto-director starting (once=%s self_directed=%s interval=%ds)',
        once,
        self_directed,
        CYCLE_INTERVAL,
    )

    cycle = 0
    while not _stop:
        cycle += 1
        log.info('=== CYCLE %d ===', cycle)
        try:
            should_continue = await run_cycle(
                fleet,
                factory,
                sessions,
                goals,
                self_directed=self_directed,
            )
        except Exception:
            log.error('Cycle %d failed, continuing...', cycle)
            should_continue = True

        if once or _stop or not should_continue:
            break

        log.info('Sleeping %ds until next cycle...', CYCLE_INTERVAL)
        await asyncio.sleep(CYCLE_INTERVAL)

    log.info('motto-director stopped after %d cycles', cycle)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='motto-director intent-driven loop')
    parser.add_argument('--once', action='store_true', help='Run one cycle and exit')
    parser.add_argument('--self-directed', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    asyncio.run(main(once=args.once, self_directed=args.self_directed))
