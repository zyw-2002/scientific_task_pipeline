"""Shared repository metadata cache, host-scoped credentials and provider cooldowns.

Adapted from citation_harbor_pipeline stage06 resource_evidence.Reader and
predownload.repository_dataset; failures remain separate from missing resources.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline_runtime import setting


MAX_METADATA_BYTES = setting('metadata_max_mb') * 1024**2
TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}


def safe_url(url):
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ''
    if parts.port:
        host += ':' + str(parts.port)
    query = [(k, 'REDACTED' if re.search(r'token|key|secret|signature|password', k, re.I) else v)
             for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, urllib.parse.urlencode(query), ''))


def redact(message, token=''):
    text = str(message)
    if token:
        text = text.replace(token, '[REDACTED]')
    text = re.sub(r'https?://[^\s<>"\']+', lambda m: safe_url(m[0]), text)
    text = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)', '[REDACTED]', text)
    return text[:400]


class ProviderError(ValueError):
    def __init__(self, status, message, **details):
        super().__init__(message)
        self.status = status
        self.details = {'status': status, 'error': message, **details}


def read_github_token(path=None):
    values = {}
    if path is not None and Path(path).is_file():
        for line in Path(path).read_text().splitlines():
            match = re.match(r'\s*(?:export\s+)?(GITHUB_TOKEN|GH_TOKEN)\s*=\s*(.*?)\s*$', line)
            if match:
                value = match[2].strip()
                if value.startswith(('"', "'")) and value.endswith(value[:1]):
                    value = value[1:-1]
                else:
                    value = value.split(' #', 1)[0].strip()
                values[match[1]] = value
    token = (values.get('GITHUB_TOKEN') or values.get('GH_TOKEN') or
             os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN') or '').strip()
    if token and (not token.isascii() or any(char.isspace() for char in token)):
        raise ValueError('GitHub token contains invalid whitespace or non-ASCII characters')
    return token


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def error_details(error, url, *, token='', read_body=False, now=None):
    now = time.time() if now is None else now
    headers = error.headers or {}
    message = str(error.reason)
    if read_body:
        try:
            body = error.read(4096)
            payload = json.loads(body)
            if isinstance(payload, dict) and isinstance(payload.get('message'), str):
                message = payload['message']
        except (OSError, ValueError):
            pass
    remaining, reset = number(headers.get('X-RateLimit-Remaining')), number(headers.get('X-RateLimit-Reset'))
    retry_after = headers.get('Retry-After')
    delay = number(retry_after)
    if retry_after and delay is None:
        try:
            delay = parsedate_to_datetime(retry_after).timestamp() - now
        except (TypeError, ValueError, OverflowError):
            pass
    throttled = error.code == 429 or (error.code == 403 and (
        remaining == 0 or retry_after is not None or re.search(r'rate.?limit|secondary rate|abuse detection', message, re.I)))
    details = {'http_status': error.code, 'provider': urllib.parse.urlsplit(url).hostname,
               'request_url': safe_url(url), 'response_message': redact(message, token),
               'rate_limit_remaining': remaining, 'rate_limit_reset': reset,
               'retry_after_seconds': delay,
               'request_id': headers.get('X-GitHub-Request-Id') or headers.get('CF-Ray'),
               'server': headers.get('Server'), 'content_type': headers.get('Content-Type'),
               'challenge': headers.get('cf-mitigated')}
    if throttled:
        resume = max(now + max(1, delay or 0), reset or 0) if delay is not None or remaining == 0 and reset else now + setting('provider_cooldown_sec')
        details.update(status='rate_limited', retryable=True, retry_at=resume)
    elif error.code == 401:
        details.update(status='authentication_failed', retryable=False)
    elif error.code == 403:
        details.update(status='access_denied', retryable=False)
    elif error.code in {404, 410}:
        details.update(status='not_found_http', retryable=False)
    elif error.code in TRANSIENT_HTTP:
        details.update(status='provider_unavailable', retryable=True, retry_at=now + max(1, delay or setting('provider_cooldown_sec')))
    else:
        details.update(status='http_error', retryable=False)
    details['error'] = f'HTTP {error.code}: {redact(message, token)}'
    if details.get('retry_at'):
        details['retry_at_utc'] = datetime.fromtimestamp(details['retry_at'], timezone.utc).isoformat()
    return details


def figshare_identity(url):
    parts = urllib.parse.urlsplit(url)
    if parts.hostname in {'doi.org', 'dx.doi.org'}:
        match = re.fullmatch(r'/10\.\d+/(?:m9\.)?figshare(?:\.plus)?\.(\d+)(?:\.v(\d+))?/?', parts.path, re.I)
    elif (parts.hostname == 'figshare.com' or (parts.hostname or '').endswith('.figshare.com')) and '/articles/' in parts.path:
        # A final /ID/VERSION is distinct from /ID or /ID/versions/VERSION.
        match = re.search(r'/(\d+)(?:/(?:versions/)?(\d+))?/?$', parts.path)
    else:
        match = None
    if not match:
        raise ProviderError('needs_review_url', 'Cannot identify a Figshare article and optional version', retryable=False)
    return match[1], match[2]


class MetadataClient:
    def __init__(self, open_url, write_json, *, cache_dir=None, token='', interval=None,
                 ttl=None, user_agent='NatureBenchMaterialDownloader/3.0'):
        self.open_url, self.write_json = open_url, write_json
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.token, self.user_agent = token, user_agent
        self.interval = setting('github_min_interval') if interval is None else interval
        self.ttl = setting('metadata_cache_ttl_sec') if ttl is None else ttl
        self._lock = threading.Lock()
        self._hosts, self._memory, self._limits, self._last = {}, {}, {}, {}
        self.network_calls = self.cache_hits = self.deferred_calls = 0

    def report(self):
        return {'github_authenticated': bool(self.token), 'network_calls': self.network_calls,
                'cache_hits': self.cache_hits, 'deferred_calls': self.deferred_calls,
                'github_metadata_concurrency': 1, 'github_min_interval_seconds': self.interval}

    def scope(self, url):
        host = urllib.parse.urlsplit(url).hostname
        identity = hashlib.sha256(self.token.encode()).hexdigest() if host == 'api.github.com' and self.token else 'public'
        return host, hashlib.sha256(f'{host}|{identity}'.encode()).hexdigest()

    @contextmanager
    def host_lock(self, scope):
        with self._lock:
            lock = self._hosts.setdefault(scope, threading.Lock())
        with lock:
            if self.cache_dir is None:
                yield
            else:
                directory = self.cache_dir / 'providers'
                directory.mkdir(parents=True, exist_ok=True)
                with (directory / f'{scope}.lock').open('a') as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    yield

    def read_cache(self, key):
        cached = self._memory.get(key)
        path = self.cache_dir / 'responses' / f'{key}.json' if self.cache_dir else None
        if cached is None and path is not None and path.is_file():
            try:
                cached = json.loads(path.read_text())
            except (OSError, ValueError):
                return None
        if cached and cached.get('expires_at', 0) > time.time() and isinstance(cached.get('body'), (dict, list)):
            digest = hashlib.sha256(json.dumps(cached['body'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if digest == cached.get('body_sha256'):
                self.cache_hits += 1
                return cached['body']
        return None

    def cooldown(self, scope):
        path = self.cache_dir / 'providers' / f'{scope}.json' if self.cache_dir else None
        if path is not None and path.is_file():
            value = json.loads(path.read_text())
            self._limits[scope] = value
        return self._limits.get(scope, {})

    def block(self, scope, details):
        self._limits[scope] = dict(details)
        if self.cache_dir:
            self.write_json(self.cache_dir / 'providers' / f'{scope}.json', details)

    def get(self, url, timeout, retries, *, allow_list=False):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
            raise ProviderError('needs_review_url', 'Metadata URL must be HTTP(S) without embedded credentials', retryable=False)
        host, scope = self.scope(url)
        key = hashlib.sha256(f'{scope}|{url}'.encode()).hexdigest()
        with self.host_lock(scope):
            cached = self.read_cache(key)
            if cached is not None:
                if isinstance(cached, list) and not allow_list:
                    raise ProviderError('metadata_error', 'metadata response is not a JSON object', retryable=False)
                return cached
            blocked = self.cooldown(scope)
            if blocked.get('status') == 'authentication_failed' or blocked.get('retry_at', 0) > time.time():
                self.deferred_calls += 1
                detail = {**blocked, 'request_url': safe_url(url), 'deferred_without_request': True}
                raise ProviderError(detail.pop('status'), detail.pop('error'), **detail)
            for attempt in range(retries + 1):
                if host == 'api.github.com':
                    delay = self.interval - (time.monotonic() - self._last.get(scope, 0))
                    if delay > 0:
                        time.sleep(delay)
                request = urllib.request.Request(url, headers={'User-Agent': self.user_agent, 'Accept': 'application/json'})
                if parts.scheme == 'https' and host == 'api.github.com' and self.token:
                    request.add_unredirected_header('Authorization', 'Bearer ' + self.token)
                self._last[scope] = time.monotonic()
                self.network_calls += 1
                try:
                    with self.open_url(request, timeout) as response:
                        headers = response.headers
                        if host == 'api.github.com' and number(headers.get('X-RateLimit-Remaining')) == 0:
                            reset = number(headers.get('X-RateLimit-Reset'))
                            self.block(scope, {'status': 'rate_limited', 'error': 'GitHub API quota exhausted',
                                'provider': host, 'retryable': True, 'retry_at': max(time.time() + 1, reset or time.time() + setting('provider_cooldown_sec')),
                                'rate_limit_remaining': 0, 'rate_limit_reset': reset})
                        raw = response.read(MAX_METADATA_BYTES + 1)
                    if len(raw) > MAX_METADATA_BYTES:
                        raise ProviderError('metadata_error', f'metadata response exceeds {MAX_METADATA_BYTES} bytes', retryable=False)
                    try:
                        value = json.loads(raw)
                    except (ValueError, UnicodeError):
                        raise ProviderError('metadata_error', 'metadata endpoint did not return valid JSON',
                                            provider=host, retryable=False) from None
                    if not isinstance(value, (dict, list) if allow_list else dict):
                        raise ProviderError('metadata_error', 'metadata response is not a JSON object', retryable=False)
                    cached = {'body': value, 'fetched_at': time.time(), 'expires_at': time.time() + self.ttl,
                              'body_sha256': hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
                    self._memory[key] = cached
                    if self.cache_dir:
                        self.write_json(self.cache_dir / 'responses' / f'{key}.json', cached)
                    return value
                except urllib.error.HTTPError as error:
                    details = error_details(error, url, token=self.token, read_body=True)
                    error.close()
                    if details['status'] in {'rate_limited', 'provider_unavailable'} or (
                        host == 'api.github.com' and details['status'] == 'authentication_failed'
                    ):
                        self.block(scope, details)
                    raise ProviderError(details.pop('status'), details.pop('error'), **details) from None
                except ProviderError:
                    raise
                except (OSError, ValueError, urllib.error.URLError) as error:
                    if attempt < retries:
                        time.sleep(min(2 ** attempt, setting('metadata_backoff_max_sec')))
                        continue
                    details = {'status': 'provider_unavailable', 'error': redact(error, self.token),
                               'provider': host, 'request_url': safe_url(url), 'retryable': True,
                               'retry_at': time.time() + setting('provider_cooldown_sec')}
                    self.block(scope, details)
                    raise ProviderError(details.pop('status'), details.pop('error'), **details) from None


def add_arguments(parser):
    parser.add_argument('--github-env-file', type=Path,
                        default=setting('stage1_env_file'))
    parser.add_argument('--metadata-cache-dir', type=Path)
    parser.add_argument('--github-min-interval', type=float, default=setting('github_min_interval'))


def validate_arguments(args):
    if not math.isfinite(args.github_min_interval) or not 0 <= args.github_min_interval <= 60:
        raise ValueError('github-min-interval must be finite and between 0 and 60 seconds')
