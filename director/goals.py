import json
import os
from pathlib import Path

GOALS_FILE = Path(os.getenv('GOALS_FILE', str(Path(__file__).parent.parent / 'goals.json')))


class GoalStore:
    def __init__(self, path: Path = GOALS_FILE):
        self.path = path
        self._data: dict = {}
        self._load()

    def _load(self):
        if self.path.exists():
            with open(self.path) as f:
                self._data = json.load(f)
        else:
            self._data = {'goals': []}

    def _save(self):
        with open(self.path, 'w') as f:
            json.dump(self._data, f, indent=2)

    def get_active_goals(self) -> list[dict]:
        return [g for g in self._data.get('goals', []) if g.get('status') != 'done']

    def get_all_goals(self) -> list[dict]:
        return self._data.get('goals', [])

    def get_goal(self, goal_id: str) -> dict | None:
        for g in self._data.get('goals', []):
            if g['id'] == goal_id:
                return g
        return None

    def update_goal_status(self, goal_id: str, status: str, notes: str = None):
        for g in self._data.get('goals', []):
            if g['id'] == goal_id:
                g['status'] = status
                if notes:
                    g['notes'] = notes
                break
        self._save()

    def mark_goal_done(self, goal_id: str, notes: str = None):
        self.update_goal_status(goal_id, 'done', notes)
