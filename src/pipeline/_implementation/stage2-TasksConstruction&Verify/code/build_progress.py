"""Persist phase boundaries without copying model/tool output into the main log."""
from datetime import datetime, timezone
import json
from pathlib import Path
import time

PREFIX = 'STAGE2_PROGRESS '


class BuildProgress:
    def __init__(self, root, state, save, *, construct_limit, review_limit, construction_only):
        self.state, self.save = state, save
        self.started = time.monotonic()
        self.current = None
        self.attempts = {'construct_worker':0, 'review_worker':0}
        self.path = Path(root) / ('metadata' if state.get('output_layout') == 'compact' else '') / 'progress.jsonl'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.common = {'schema_version':'stage2-progress-v1', 'construct_limit':construct_limit,
                       'review_limit':review_limit,
                       'scope':'task_package_static' if construction_only else 'execution'}
        self.emit('workflow', 'start')

    def emit(self, phase, status, **values):
        event = {**self.common, 'timestamp':datetime.now(timezone.utc).isoformat(),
                 'phase':phase, 'status':status, 'elapsed_seconds':round(time.monotonic()-self.started, 3), **values}
        with self.path.open('a') as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + '\n')
        self.state['progress'] = event
        self.save()
        print(PREFIX + json.dumps(event, ensure_ascii=False), flush=True)

    def start(self, phase, attempt, limit):
        if self.current is not None: raise RuntimeError('previous progress phase was not finished')
        if phase in self.attempts: self.attempts[phase] = max(self.attempts[phase], attempt)
        self.current = (phase, attempt, limit, time.monotonic())
        self.emit(phase, 'start', attempt=attempt, limit=limit, phase_elapsed_seconds=0)

    def finish(self, status='pass', **values):
        if self.current is None: return
        phase, attempt, limit, started = self.current
        elapsed = round(time.monotonic()-started, 3)
        self.state.setdefault('phase_timings', []).append(
            {'phase':phase, 'attempt':attempt, 'status':status, 'seconds':elapsed})
        self.emit(phase, status, attempt=attempt, limit=limit, phase_elapsed_seconds=elapsed, **values)
        self.current = None

    def complete(self):
        if self.current is not None: self.finish('fail')
        self.emit('workflow', self.state['status'],
                  construct_attempts=self.attempts['construct_worker'],
                  review_attempts=self.attempts['review_worker'],
                  execution_ready=bool(self.state.get('execution_ready')))
