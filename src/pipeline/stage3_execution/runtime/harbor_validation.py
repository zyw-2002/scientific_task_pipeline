"""Validate run-owned task structure with the exact installed Harbor parser."""
import json
from pathlib import Path
import shlex
import shutil
import subprocess

CHECK = '''
import json,sys
from pathlib import Path
from harbor.models.task.config import TaskConfig
from harbor.models.task.paths import TaskPaths
from harbor.models.task.task import Task
p=Path(sys.argv[1])
try:
    config=TaskConfig.model_validate_toml((p/'task.toml').read_text())
    if not (p/'environment').is_dir():
        raise ValueError('Missing environment directory')
    Task._validate_tests(config,TaskPaths(p))
    print(json.dumps({'status':'pass'}))
except Exception as exc:
    print(json.dumps({'status':'fail','error':str(exc)}))
    sys.exit(2)
'''


def validate_task(package, harbor):
    executable = shutil.which(str(harbor))
    if executable is None:
        raise ValueError('Harbor executable is unavailable: ' + str(harbor))
    with Path(executable).open('rb') as stream:
        line = stream.readline(4096).decode(errors='replace').strip()
    if not line.startswith('#!'):
        raise ValueError('Cannot resolve the Python interpreter for Harbor task validation')
    interpreter = shlex.split(line[2:])
    if not interpreter:
        raise ValueError('Harbor executable has an empty interpreter declaration')
    try:
        result = subprocess.run([*interpreter, '-c', CHECK, str(Path(package).resolve())],
                                capture_output=True, text=True, timeout=30)
        report = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ValueError('Harbor task validation could not complete: ' + type(exc).__name__) from exc
    if result.returncode or report.get('status') != 'pass':
        raise ValueError('Harbor rejected the prepared task: ' + report.get('error', result.stderr[-500:]))
    return report
