"""Fresh authoring workers; no task code is imported or executed by the host."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import re
import shlex
import subprocess
import sys
import uuid

STAGE1_CODE = Path(__file__).resolve().parents[2] / 'stage1-datacard&downlond' / 'code'
sys.path.append(str(STAGE1_CODE))
from network_config import resolve_network, network_for_policy, make_compose_network_overlay, ENV_NAMES


class AuthoringWorkerError(ValueError):
    """Machine-readable worker failure; never treat partial artifacts as success."""
    def __init__(self, code, job, *, details=None):
        self.code, self.job = code, Path(job)
        self.details = details or {}
        self.retryable = code in {'agent_loop_limit', 'tmux_session_lost',
                                  'container_runtime_lost', 'environment_start_failed'}
        if (code == 'harbor_trial_failed' and self.details.get('trial_count') == 1
                and self.details.get('job_finished') is True
                and self.details.get('exception_types') == ['AgentTimeoutError']):
            # This remains a failed trial. compact_outputs only permits
            # recovery when exactly one safe partial package is preserved;
            # pipeline.py additionally checks frozen inputs and retry budget.
            self.retryable = True
        descriptions = {
            'agent_loop_limit': 'agent stopped by loop detection / per-turn tool-call limit',
            'tmux_session_lost': 'the authoring terminal disappeared while sending keys',
            'container_runtime_lost': 'the authoring container stopped while the agent was running',
            'environment_start_failed': 'Harbor could not start the assigned environment',
            'harbor_trial_failed': 'Harbor trial failed; inspect saved worker logs',
            'ambiguous_artifacts': 'Harbor produced an ambiguous number of trial results',
            'harbor_incomplete': 'Harbor did not record job completion',
            'worker_exit': 'authoring worker exited unsuccessfully',
            'missing_workspace': 'worker workspace artifact is missing',
        }
        super().__init__(f'{code}: {descriptions.get(code, code)}; job: {self.job}')


def harbor_failure(job, summary, trials, returncode):
    """Extract safe diagnostics. Do not print raw exceptions, prompts or credentials."""
    details = {'returncode': returncode, 'trial_count': len(trials),
               'job_finished': bool(summary.get('finished_at')),
               'errored_trials': summary.get('stats', {}).get('n_errored_trials')}
    exception_types = []
    terminal_lost = False
    container_runtime_lost = False
    start_failure = None
    for path in trials:
        try:
            info = read_json(path).get('exception_info') or {}
            name = info.get('exception_type', '')
            message = info.get('exception_message', '')
            project = path.parent.name.lower() + '__env'
            if name == 'EnvironmentStartTimeoutError':
                start_failure = 'environment_start_timeout'
            elif (name == 'RuntimeError' and isinstance(message, str)
                  and f'--project-name {project} ' in message and ' up --detach --wait' in message):
                if re.search(r'Error response from daemon: Conflict\. The container name "/' + re.escape(project)
                             + r'-[a-zA-Z0-9_.-]+" is already in use by container "[0-9a-f]{12,64}"', message):
                    start_failure = 'compose_project_container_conflict'
                elif (re.search(r'\bContainer\s+' + re.escape(project) + r'-[\w.-]+\s+Starting\b', message)
                      and re.search(r'(?m)^Error response from daemon: No such container: [0-9a-f]{12,64}\s*$', message)):
                    start_failure = 'compose_container_lost_at_start'
            if name == 'RuntimeError' and isinstance(message, str) and re.search(
                    r"failed to send (?:non-blocking|blocking) keys: command='tmux send-keys .*?"
                    r"return_code=1, (?:stderr|stdout)=(?:None, (?:stderr|stdout)=)?'no server running on /",
                    message):
                terminal_lost = True
            # Harbor sometimes reports a container disappearance directly from
            # docker-compose, without the tmux "no server running" diagnostic.
            # Treat both forms as infrastructure failures so a fresh worker can
            # be started from the frozen task inputs.
            if isinstance(message, str) and (
                    re.search(r'\bservice\s+"main"\s+is\s+not\s+running\b', message, re.I)
                    or re.search(r'Error response from daemon:\s*Container\s+[0-9a-f]{12,64}\s+is\s+not\s+running\b', message, re.I)):
                container_runtime_lost = True
            if isinstance(name, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]{0,119}', name):
                exception_types.append(name)
        except (OSError, ValueError, AttributeError):
            continue
    details['exception_types'] = exception_types
    # Qwen's actual terminal diagnostic is kept in qwen-code.txt or job.log.
    # Require a line prefix, rather than matching a phrase anywhere in a prompt.
    loop = False
    for path in [*job.glob('*/agent/qwen-code.txt'), job / 'job.log']:
        if not path.is_file():
            continue
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 65536))
            tail = stream.read().decode('utf-8', errors='replace')
        if re.search(r'(?m)^(?:stdout: )?Loop detection halted the run\b', tail):
            loop = True
    if len(trials) != 1:
        code = 'ambiguous_artifacts'
    elif start_failure and details['errored_trials'] == 1:
        code = 'environment_start_failed'
        details['failure_class'] = 'infrastructure_fail'
        details['start_failure'] = start_failure
    elif terminal_lost:
        code = 'tmux_session_lost'
        details['failure_class'] = 'infrastructure_fail'
    elif container_runtime_lost:
        code = 'container_runtime_lost'
        details['failure_class'] = 'infrastructure_fail'
    elif loop:
        code = 'agent_loop_limit'
    elif details['errored_trials']:
        code = 'harbor_trial_failed'
    elif not details['job_finished']:
        code = 'harbor_incomplete'
    else:
        code = 'worker_exit'
    return AuthoringWorkerError(code, job, details=details)


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    os.replace(temp, path)


def read_json(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'duplicate JSON key: {key}')
            result[key] = value
        return result
    value = json.loads(path.read_text(), object_pairs_hook=unique,
                       parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
    if not isinstance(value, dict):
        raise ValueError(f'expected JSON object: {path}')
    return value


def model_process_env(config: dict, base: dict) -> dict:
    """Load explicit credentials into the child process, never job JSON/argv.

    Env files contain literal KEY=value lines; no shell execution/interpolation.
    CLI endpoint/key selection wins over the file. Data proxy interpolation
    stays pinned to network_config's explicit PIPELINE_* environment.
    """
    env = dict(base)
    if config.get('env_file'):
        for line in Path(config['env_file']).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:].lstrip()
            key, sep, value = line.partition('=')
            if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key.strip()):
                raise ValueError('model env file requires literal KEY=value lines')
            tokens = shlex.split(value, comments=True)
            if len(tokens) > 1:
                raise ValueError(f'model env file value must be quoted: {key.strip()}')
            if key.strip() not in ENV_NAMES and not key.strip().startswith(('PIPELINE_DATA_', 'PIPELINE_BUILD_')):
                env[key.strip()] = tokens[0] if tokens else ''
    key_name = config.get('api_key_env')
    if key_name:
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key_name):
            raise ValueError('api_key_env must name an environment variable')
        if not env.get(key_name):
            raise ValueError(f'model API credential environment variable is unset: {key_name}')
        env['OPENAI_API_KEY'] = env[key_name]
    if config.get('endpoint'):
        from urllib.parse import urlsplit
        endpoint = config['endpoint']
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('model endpoint must be an HTTP(S) URL without credentials/query')
        env['OPENAI_BASE_URL'] = endpoint
    return env


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def snapshot(root: Path, exclude=()) -> dict:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f'not a regular directory: {root}')
    result = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        if relative in exclude:
            continue
        if path.is_symlink():
            raise ValueError(f'symlink not allowed in frozen input: {path}')
        if path.is_file():
            result[relative] = file_sha256(path)
        elif not path.is_dir():
            raise ValueError(f'special file not allowed: {path}')
    return result


def digest_tree(root: Path, exclude=()) -> str:
    return hashlib.sha256(json.dumps(snapshot(root, exclude), sort_keys=True).encode()).hexdigest()


def copy_tree(source: Path, target: Path) -> None:
    source, target = source.resolve(), target.resolve()
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError('source/output trees must be separate')
    before = snapshot(source)
    shutil.copytree(source, target)
    if snapshot(target) != before or snapshot(source) != before:
        raise ValueError('source changed during copy')


def timeout_seconds(value, name: str = 'timeout') -> float:
    """Validate elapsed-time limits; zero explicitly disables the limit."""
    if isinstance(value, bool):
        raise ValueError(f'{name} must be a finite number >= 0; 0 means unlimited')
    try:
        seconds = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f'{name} must be a finite number >= 0; 0 means unlimited') from error
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f'{name} must be a finite number >= 0; 0 means unlimited')
    return seconds


def run_logged(command: list[str], log: Path, *, cwd: Path, timeout: float | None, env=None) -> int:
    """Stream logs; 0/None waits without a deadline, with interrupt cleanup."""
    if not command or not all(isinstance(x, str) and x for x in command):
        raise ValueError('command must be a nonempty argv array')
    wait_timeout = None if timeout is None else timeout_seconds(timeout) or None
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('wb') as stream:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=stream,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return proc.wait(timeout=wait_timeout)
        except BaseException:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
            raise


def make_role_task(output: Path, role: str, paths: dict, prompt: str,
                   artifact_name: str, config: dict, *, metadata: dict | None = None) -> Path:
    """Caller populates environment/input first. Each role gets a new task."""
    artifacts = {'construct': 'build', 'construct_verify': 'verified'}
    if role not in artifacts:
        raise ValueError('unsupported authoring role; judge has no tools')
    if artifact_name != artifacts[role]:
        raise ValueError('unexpected authoring artifact for this role')
    image = config.get('base_image', 'naturebench-base:v3')
    if not isinstance(image, str) or not image or any(c.isspace() for c in image):
        raise ValueError('invalid authoring base_image')
    from authoring_package import validate_paths
    paths = validate_paths({**paths, 'output_dir': '/workspace/' + artifact_name})
    execution_id = uuid.uuid4().hex
    atomic_json(output / 'environment/input/paths.json', paths)
    # Execution metadata stays on the host, outside the container input mount.
    atomic_json(output / 'worker_state.json', {
        'execution_id': execution_id, 'role': role, 'metadata': metadata or {},
        'output_dir': paths['output_dir']})
    overview, marker, reference = prompt.partition('\n<!-- stage2-role-reference -->\n')
    if marker:
        reference_path = output / 'environment/input/role_reference.md'
        reference_path.write_text(reference, encoding='utf-8')
        # Point the persistent instruction at specific line ranges in the full specification.
        headings = []
        fenced = False
        for line_number, line in enumerate(reference.splitlines(), 1):
            if line.startswith('```'):
                fenced = not fenced
            elif not fenced and line.startswith(('# ', '## ', '### ')):
                headings.append(f'{line_number}  {line.lstrip("# ")}')
        prompt = overview + '\n\n完整角色规范见 `/task/input/role_reference.md`。依当前步骤按行查阅相关章节。\n\n' + '\n'.join(headings)
    (output / 'instruction.md').write_text(prompt + '\n\n'
        '读取 `/task/input/paths.json` 获取材料与输出位置，在 output_dir 交付文件；新生成内容使用英文。\n')
    (output / 'environment/Dockerfile').write_text(
        f'FROM {image}\nWORKDIR /workspace\nCOPY input /task/input\n')
    timeout = timeout_seconds(config.get('timeout_sec', 7200), 'timeout_sec')
    cpu, memory = int(config.get('cpus', 4)), int(config.get('memory_mb', 16384))
    gpus = int(config.get('gpus', 0))
    build_timeout = timeout_seconds(config.get('build_timeout_sec', 7200), 'build_timeout_sec')
    network = config.get('network_mode', 'public')
    if cpu < 1 or memory < 1 or gpus < 0 or network not in {'public', 'no-network', 'allowlist'}:
        raise ValueError('invalid authoring resources/network')
    proxy = network_for_policy(config.get('network'), network)
    compose = make_compose_network_overlay({'main': False}, proxy)
    compose['services']['main'].update(image=image, working_dir='/workspace', entrypoint=[],
        command=['sleep', 'infinity'], volumes=[{'type':'bind', 'source':'./input',
        'target':'/task/input', 'read_only':True}])
    compose['services']['main']['network_mode'] = 'none' if network == 'no-network' else 'bridge'
    (output / 'environment/docker-compose.yaml').write_text(json.dumps(compose, indent=2) + '\n')
    hosts = config.get('allowed_hosts', [])
    if network == 'allowlist' and not hosts:
        raise ValueError('authoring allowlist requires allowed_hosts')
    host_line = f'allowed_hosts = {json.dumps(hosts)}\n' if network == 'allowlist' else ''
    (output / 'task.toml').write_text(
        'schema_version = "1.4"\n'
        f'artifacts = [{{source = "/workspace/{artifact_name}", service = "main"}}]\n'
        f'[task]\nname = "pipeline/{role}-{execution_id[:12]}"\n'
        f'[agent]\ntimeout_sec = {timeout}\n'
        f'[environment]\ndocker_image = {json.dumps(image)}\ncpus = {cpu}\nmemory_mb = {memory}\ngpus = {gpus}\nbuild_timeout_sec = {build_timeout}\n'
        f'network_mode = "{network}"\n{host_line}'
        '[verifier]\ntimeout_sec = 60.0\n')
    tests = output / 'tests'
    tests.mkdir(exist_ok=True)
    (tests / 'test.sh').write_text('#!/bin/sh\n# Authoring uses host collection, not a scientific score.\nexit 1\n')
    (tests / 'test.sh').chmod(0o755)
    atomic_json(output / 'worker_snapshot.json', snapshot(output))
    return output


def verify_role_task(task: Path) -> dict:
    expected = read_json(task / 'worker_snapshot.json')
    if expected != snapshot(task, exclude={'worker_snapshot.json'}):
        raise ValueError('authoring files or input changed after preparation')
    return {**read_json(task / 'worker_state.json'), 'paths': read_json(task / 'environment/input/paths.json')}


def execute_role(task: Path, jobs: Path, config: dict) -> Path:
    state = verify_role_task(task)
    jobs.mkdir(parents=True, exist_ok=True)
    name = state['role'] + '-' + uuid.uuid4().hex[:12]
    backend = config.get('backend', 'harbor')
    role_timeout = timeout_seconds(config.get('timeout_sec', 7200), 'timeout_sec')
    build_timeout = timeout_seconds(config.get('build_timeout_sec', 7200), 'build_timeout_sec')
    unlimited = role_timeout == 0 or build_timeout == 0
    timeout = None if unlimited else role_timeout + build_timeout + 300
    network = network_for_policy(config.get('network'), config.get('network_mode', 'public')).with_bypass(['main'])
    build_network = resolve_network(config.get('build_network', config.get('network')))
    process_env = model_process_env(config, network.process_env(build_network=build_network))
    if backend == 'command':
        # An explicitly configured trusted host adapter, primarily for local mocks.
        workspace = jobs / name / 'workspace'
        workspace.mkdir(parents=True)
        values = {'task': str(task.resolve()), 'paths': str((task / 'environment/input/paths.json').resolve()),
                  'workspace': str(workspace.resolve()), 'role': state['role']}
        command = config.get('command')
        if not isinstance(command, list):
            raise ValueError('command backend requires an argv array')
        command = [v.format_map(values) for v in command]
        rc = run_logged(command, jobs / name / 'worker.log', cwd=workspace, timeout=timeout, env=process_env)
    elif backend == 'harbor':
        if not config.get('model'):
            raise ValueError('Harbor worker requires a model in worker config')
        agent_kwargs = config.get('agent_kwargs') or {}
        if not isinstance(agent_kwargs, dict):
            raise ValueError('agent_kwargs must be an object in worker config')
        if any(not isinstance(key, str) or not key for key in agent_kwargs):
            raise ValueError('agent_kwargs keys must be nonempty strings')
        # A start timeout occurs before the agent runs. Give Harbor one fresh
        # start and leave the full agent deadline available after it succeeds.
        start_retry_wait = 15
        if timeout is not None:
            timeout += build_timeout + start_retry_wait
        job_config = jobs / (name + '.json')
        atomic_json(job_config, {'job_name': name, 'jobs_dir': str(jobs.resolve()),
            'n_attempts': 1, 'n_concurrent_trials': 1,
            'retry': {'max_retries': 1, 'include_exceptions': ['EnvironmentStartTimeoutError'],
                      'min_wait_sec': start_retry_wait},
            # Installation/setup is part of an unlimited authoring run too.
            **({'agent_setup_timeout_multiplier': 0} if unlimited else {}),
            'environment': {'type': 'docker', 'delete': True},
            'agents': [{'name': config.get('agent', 'qwen-coder'), 'model_name': config['model'],
                        'kwargs': {**agent_kwargs, **({'base_url': config['endpoint']} if config.get('endpoint') else {})}}],
            'tasks': [{'path': str(task.resolve())}]})
        command = [config.get('harbor', 'harbor'), 'run', '-c', str(job_config.resolve()),
                   '--max-revisions', '0', '--disable-verification', '--yes']
        rc = run_logged(command, jobs / (name + '.log'), cwd=jobs, timeout=timeout, env=process_env)
        job = jobs / name
        summary = read_json(job / 'result.json') if (job / 'result.json').is_file() else {}
        trials = list(job.glob('*/result.json'))
        if not summary.get('finished_at') or summary.get('stats', {}).get('n_errored_trials') or len(trials) != 1 or rc != 0:
            raise harbor_failure(job, summary, trials, rc)
        workspace = trials[0].parent / 'artifacts/workspace'
    else:
        raise ValueError('backend must be harbor or command')
    verify_role_task(task)
    if rc != 0:
        raise AuthoringWorkerError('worker_exit', jobs / name, details={'returncode': rc})
    if not workspace.is_dir():
        raise AuthoringWorkerError('missing_workspace', jobs / name)
    return workspace
