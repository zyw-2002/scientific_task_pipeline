"""Read Harbor's trial outcome separately from the CLI process exit code."""
import json
from pathlib import Path


def trial_outcome(trial: Path) -> dict:
    result = json.loads((Path(trial) / 'result.json').read_text())
    exception = result.get('exception_info')
    if exception:
        return {'execution_status': 'failed',
                'failure': exception.get('exception_type', 'HarborTrialError')}
    if not result.get('finished_at'):
        return {'execution_status': 'failed', 'failure': 'HarborTrialIncomplete'}
    return {'execution_status': 'completed'}
