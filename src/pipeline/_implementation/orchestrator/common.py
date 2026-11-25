"""Small shared primitives for the four-stage experiment driver."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import time
import threading
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), object_pairs_hook=unique,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))


def rows(path):
    values = []
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        if line.strip():
            item = json.loads(line, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
            if not isinstance(item, dict):
                raise ValueError(f'{path}:{number}: expected JSON object')
            values.append(item)
    return values


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    temporary.replace(path)


def write_rows(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.tmp')
    temporary.write_text(''.join(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n' for row in values))
    temporary.replace(path)


def slug(value):
    value = str(value)
    return (re.sub(r'[^A-Za-z0-9._-]', '-', value).strip('.-')[:65] or 'item') + '-' + hashlib.sha256(value.encode()).hexdigest()[:10]


def load_model_environment(path, key_name, base=None, extra_keys=()):
    """Read named provider settings without evaluating a shell file."""
    env = dict(os.environ if base is None else base)
    allowed = {'OPENAI_API_KEY', 'OPENROUTER_API_KEY', 'ANTHROPIC_API_KEY', key_name,
               'OPENAI_BASE_URL', 'ANTHROPIC_BASE_URL', 'BASE_URL', 'MODEL_NAME', 'OPENAI_MODEL', *extra_keys}
    if path:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            line = line.removeprefix('export ').strip()
            name, sep, value = line.partition('=')
            if sep and name.strip() in allowed:
                parsed = shlex.split(value, comments=True)
                if len(parsed) > 1:
                    raise ValueError('quote whitespace in model env settings: ' + name.strip())
                env[name.strip()] = parsed[0] if parsed else ''
    return env


class CommandFailed(RuntimeError):
    def __init__(self, receipt):
        self.receipt = receipt
        super().__init__(f"{receipt['step']} failed with exit {receipt.get('returncode')}; see {receipt['log']}")


class Stage2ProgressRelay:
    """Forward only typed phase metadata; never echo raw commands/model logs."""
    phases = {'workflow':'流程', 'construct_prepare':'01 构造输入准备',
              'construct_worker':'01 构造与容器内自检', 'construct_gate':'01 宿主规则验收',
              'review_prepare':'02 审核输入准备', 'review_worker':'02 科学审核与修复',
              'review_gate':'02 宿主构造验收', 'runtime_probe':'正式环境验收'}
    statuses = {'start':'开始', 'pass':'通过', 'fail':'未通过', 'ready':'完成',
                'blocked':'达到重试上限/阻塞', 'failed':'异常退出',
                'needs_runtime_verification':'等待运行验收', 'completed':'执行结束（验收另列）'}

    def __init__(self, log):
        self.log, self.offset, self.active = Path(log), 0, None

    def poll(self):
        if not self.log.is_file(): return
        with self.log.open(encoding='utf-8', errors='replace') as stream:
            stream.seek(self.offset)
            while True:
                start = stream.tell()
                line = stream.readline()
                if not line: break
                if not line.endswith('\n'):
                    stream.seek(start); break
                if not line.startswith('STAGE2_PROGRESS ') or len(line) > 16384: continue
                try:
                    event = json.loads(line.removeprefix('STAGE2_PROGRESS '))
                    if (not isinstance(event, dict) or event.get('schema_version') != 'stage2-progress-v1'
                            or event.get('phase') not in self.phases or event.get('status') not in self.statuses): continue
                    for key in ('elapsed_seconds','phase_elapsed_seconds','attempt','limit',
                                'construct_limit','review_limit','construct_attempts','review_attempts',
                                'failed_checks','validation_errors'):
                        if key in event and (type(event[key]) not in (int,float) or not 0 <= event[key] < 1e12):
                            raise ValueError('invalid progress number')
                    self.show(event)
                except (ValueError, KeyError, TypeError, OverflowError): continue
            self.offset = stream.tell()

    def show(self, event):
        timestamp = datetime.fromisoformat(event['timestamp'])
        if timestamp.tzinfo is None: raise ValueError('progress timestamp requires timezone')
        stamp = timestamp.astimezone().strftime('%H:%M:%S')
        phase, status = event['phase'], event['status']
        text = f"[{stamp}] [Stage2] {self.phases[phase]}"
        if event.get('attempt'):
            limit = event.get('limit') or '不限'
            text += f" 第 {event['attempt']}/{limit} 轮"
        text += '：' + self.statuses[status]
        if phase == 'workflow' and status == 'start':
            text += f"；构造最多 {event.get('construct_limit') or '不限'} 轮，审核最多 {event.get('review_limit')} 轮，通过即进入下一阶段，不必跑满"
        elif phase == 'workflow':
            text += f"；实际构造 {event.get('construct_attempts',0)} 轮、审核 {event.get('review_attempts',0)} 轮；总耗时 {event.get('elapsed_seconds',0):.1f}s"
            if status == 'ready' and event.get('scope') == 'task_package_static':
                text += '；构造验收通过，正式运行环境未验收'
            elif status == 'ready' and event.get('execution_ready') is True:
                text += '；正式运行环境验收通过'
        elif status != 'start':
            text += f"；本阶段 {event.get('phase_elapsed_seconds',0):.1f}s"
            if event.get('failed_checks') or event.get('validation_errors'):
                text += f"；规则失败 {event.get('failed_checks',0)} 项，验收错误 {event.get('validation_errors',0)} 项"
            failures = {'agent_loop_limit':'代理触发循环检测/工具调用上限',
                        'harbor_trial_failed':'Harbor 任务异常',
                        'ambiguous_artifacts':'任务产物不唯一',
                        'harbor_incomplete':'Harbor 未完成', 'worker_exit':'代理进程失败',
                        'missing_workspace':'未收集到任务包', 'worker_exception':'代理执行异常'}
            if event.get('failure_code') in failures:
                text += '；原因：' + failures[event['failure_code']]
                if event.get('retry_scheduled') is True:
                    text += '；已保存未完成任务包，将进入下一轮修复'
                else:
                    text += '；已保存诊断，本次停止'
        print(text, flush=True)
        self.active = event if status == 'start' else None

    def heartbeat(self):
        if self.active is None: return {}
        event = self.active
        started = datetime.fromisoformat(event['timestamp'])
        return {'phase':self.phases[event['phase']], 'attempt':event.get('attempt'),
                'attempt_limit':event.get('limit'),
                'phase_elapsed_seconds':max(0, round((datetime.now(timezone.utc)-started).total_seconds()))}


class Commands:
    def __init__(self, root: Path, *, env=None, resume=False, timeout=28800, executor=None, compact=False):
        self.root = Path(root).resolve()
        if timeout is not None and timeout < 0:
            raise ValueError('command timeout must be nonnegative; 0 means unlimited')
        self.env, self.resume, self.timeout, self.executor = env, resume, None if timeout == 0 else timeout, executor
        self.lock = threading.Lock()
        self.compact = compact
        if compact:
            self.receipts_path = self.root/'metadata/commands.json'
            self.receipts_path.parent.mkdir(parents=True,exist_ok=True)
            (self.root/'logs').mkdir(parents=True,exist_ok=True)
            self.log_script = None
        else:
            (self.root / 'work/receipts').mkdir(parents=True, exist_ok=True)
            (self.root / 'work/logs').mkdir(parents=True, exist_ok=True)
            self.log_script = self.root / 'commands.sh'
            if not self.log_script.exists():
                self.log_script.write_text('#!/usr/bin/env bash\n# Exact expanded commands; credentials are referenced by environment name.\nset -euo pipefail\n')

    def _save_receipt(self, path, receipt):
        if not self.compact:
            write_json(path,receipt)
            return
        with self.lock:
            saved=read_json(self.receipts_path) if self.receipts_path.exists() else {'steps':{}}
            saved['steps'][receipt['step']]=receipt
            write_json(self.receipts_path,saved)

    def run(self, step, argv, *, outputs=(), inputs=(), allowed=(0,), env=None):
        argv = [str(x) for x in argv]
        identity = {'argv': argv, 'inputs': {str(Path(p).resolve()): digest(p) for p in inputs if Path(p).is_file()}}
        key = fingerprint(identity)
        receipt_path = self.receipts_path if self.compact else self.root / 'work/receipts' / (slug(step) + '.json')
        with self.lock:
            old = (read_json(receipt_path).get('steps',{}).get(step) if self.compact else read_json(receipt_path)) if receipt_path.exists() else None
        if old is not None:
            if old.get('identity_sha256') != key:
                raise ValueError(f'{step}: command/input changed; use a new batch directory')
            if self.resume and old.get('status') in {'completed', 'partial'}:
                for path, sha in old.get('outputs', {}).items():
                    if not Path(path).is_file() or digest(path) != sha:
                        raise ValueError(f'{step}: completed output changed: {path}')
                for path, expected in old.get('trees', {}).items():
                    if tree_snapshot(Path(path)) != expected:
                        raise ValueError(f'{step}: referenced artifacts changed under {path}')
                if not all(str(Path(p).resolve()) in old.get('outputs', {}) for p in outputs):
                    raise ValueError(f'{step}: incomplete output receipt')
                print(json.dumps({'step': step, 'status': 'resumed'}, ensure_ascii=False), flush=True)
                return old
            if not self.resume:
                raise ValueError(f'{step}: previous receipt exists; use --resume or a new batch')
            # Workers themselves own recovery; no deletion of partial artifacts.
        log = self.root / ('logs' if self.compact else 'work/logs') / (slug(step) + '.log')
        receipt = {'step': step, **identity, 'identity_sha256': key, 'status': 'running',
                   'started_at': now(), 'log': str(log), 'outputs': {}}
        self._save_receipt(receipt_path, receipt)
        with self.lock:
            if self.log_script is not None:
                with self.log_script.open('a') as stream:
                    stream.write('\n# ' + step.replace('\n', ' ') + '\n' + shlex.join(argv) + '\n')
        print(json.dumps({'step': step, 'status': 'running', 'log': str(log)}, ensure_ascii=False), flush=True)
        started = time.monotonic()
        relay = Stage2ProgressRelay(log) if step.startswith('stage2.') else None
        child_env = self.env if env is None else env
        process = None
        try:
            if self.executor:
                rc = self.executor(argv, log=log, env=child_env, cwd=self.root, timeout=self.timeout)
            else:
                with log.open('ab') as stream:
                    process = subprocess.Popen(argv, cwd=self.root, env=child_env, stdout=stream,
                                               stderr=subprocess.STDOUT, start_new_session=True)
                    while True:
                        remaining = None if self.timeout is None else self.timeout - (time.monotonic() - started)
                        if remaining is not None and remaining <= 0:
                            os.killpg(process.pid, signal.SIGTERM)
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                os.killpg(process.pid, signal.SIGKILL)
                                process.wait()
                            raise TimeoutError(f'{step}: command timeout')
                        try:
                            rc = process.wait(timeout=30 if remaining is None else min(30, remaining))
                            if relay: relay.poll()
                            break
                        except subprocess.TimeoutExpired:
                            if relay: relay.poll()
                            print(json.dumps({'step': step, 'status': 'running', 'elapsed_seconds': round(time.monotonic()-started),
                                              **(relay.heartbeat() if relay else {})}, ensure_ascii=False), flush=True)
            missing = [str(p) for p in outputs if not Path(p).is_file()]
            receipt.update(returncode=rc, status='completed' if rc == 0 else 'partial' if rc in allowed else 'failed')
            if missing:
                receipt.update(status='failed', error='missing declared outputs: ' + ', '.join(missing))
            receipt['outputs'] = {str(Path(p).resolve()): digest(p) for p in outputs if Path(p).is_file()}
            receipt['trees'] = {str(Path(p).resolve().parent.parent if Path(p).name=='build.json' else Path(p).resolve().parent):tree_snapshot(Path(p).resolve().parent.parent if Path(p).name=='build.json' else Path(p).resolve().parent)
                                for p in outputs if Path(p).is_file() and Path(p).name in
                                {'result.json','build.json','pair_manifest.json','materials_manifest.json'}}
        except BaseException as error:
            if process is not None and process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGKILL)
                    process.wait()
            receipt.update(status='failed', error=str(error), returncode=None)
            raise
        finally:
            if relay: relay.poll()
            receipt.update(finished_at=now(), wall_seconds=round(time.monotonic()-started, 6))
            self._save_receipt(receipt_path, receipt)
        if receipt['status'] == 'failed':
            raise CommandFailed(receipt)
        return receipt


def tree_snapshot(root):
    result = {}
    if not root.is_dir() or root.is_symlink():
        raise ValueError('artifact tree missing or symlink: '+str(root))
    for path in sorted(root.rglob('*')):
        relative=path.relative_to(root)
        if path.is_symlink():
            # Solver workspaces can contain virtualenv links. Record the link
            # itself; never follow it outside the artifact tree. Source/task
            # packages still have their independent stricter isolation checks.
            result[relative.as_posix()]={'type':'symlink','target':os.readlink(path)}
            continue
        if '__pycache__' in relative.parts or path.suffix=='.pyc':
            continue
        if path.is_file():
            result[relative.as_posix()]=digest(path)
        elif not path.is_dir():
            raise ValueError('nonregular artifact: '+str(path))
    return result
