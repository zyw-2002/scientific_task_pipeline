"""Shared data-download network policy; a Docker bridge is not a proxy setting.

No model/search gateway URL is treated as a download proxy. Secrets are read
only from the explicitly selected env file or process and never written into
the public report / Compose overlay. Probes perform HEAD, not dataset GETs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import urllib.error
import urllib.parse
import urllib.request

PROXY_NAMES = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY')
ENV_NAMES = tuple(n for name in PROXY_NAMES for n in (name, name.lower()))
FILE_NAMES = set(ENV_NAMES) | {'PIPELINE_DATA_PROXY', 'PROXY_FOR_TOOLS'}
DEFAULT_BYPASS = 'localhost,127.0.0.1,::1,host.docker.internal,main,evaluator,eval,scorer'


def redact_url(value: str) -> str:
    try:
        u = urllib.parse.urlsplit(value)
        host = u.hostname or ''
        if ':' in host:
            host = '[' + host + ']'
        return u.scheme + '://' + host + (':' + str(u.port) if u.port else '')
    except ValueError:
        return '[invalid URL]'


def read_proxy_env(path: str | Path) -> dict[str, str]:
    """Read only proxy names, with no shell execution or variable expansion."""
    result = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        key, sep, value = line.partition('=')
        key = key.strip()
        if not sep or key not in FILE_NAMES:
            continue
        tokens = shlex.split(value, comments=True, posix=True)
        if len(tokens) > 1:
            raise ValueError(f'proxy env value must be quoted: {key}')
        result[key] = tokens[0] if tokens else ''
    return result


def _validate_proxy(value: str, *, allow_credentials=True) -> str:
    u = urllib.parse.urlsplit(value)
    if u.scheme not in {'http', 'https'} or not u.hostname or u.path not in {'', '/'} or u.query or u.fragment:
        raise ValueError('download proxies must be HTTP(S) origin URLs; SOCKS requires a separate transport')
    try:
        u.port
    except ValueError as exc:
        raise ValueError('invalid proxy port') from exc
    if not allow_credentials and (u.username or u.password):
        raise ValueError('proxy credentials must be provided via proxy_env_file or environment, not public JSON')
    if any(c in value for c in '\n\r\x00'):
        raise ValueError('invalid proxy URL characters')
    return value.rstrip('/')


class _ProxyHandler(urllib.request.ProxyHandler):
    """Honor this config's bypass list without consulting ambient os.environ."""
    def __init__(self, proxies, bypass):
        super().__init__(proxies)
        self.bypass = bypass

    def proxy_open(self, req, proxy, type):
        if urllib.request.proxy_bypass_environment(req.host, {'no': self.bypass}):
            return None
        # The stdlib implementation consults the global NO_PROXY again; perform
        # the small HTTP proxy setup directly so explicit configs stay isolated.
        scheme, user, password, hostport = urllib.request._parse_proxy(proxy)
        if user and password:
            import base64
            token = (urllib.parse.unquote(user) + ':' + urllib.parse.unquote(password)).encode()
            req.add_header('Proxy-Authorization', 'Basic ' + base64.b64encode(token).decode())
        original_type = req.type
        req.set_proxy(urllib.parse.unquote(hostport), scheme or original_type)
        if original_type == scheme or original_type == 'https':
            return None
        return self.parent.open(req, timeout=req.timeout)


class NetworkConfig:
    def __init__(self, env: dict[str, str], source: str):
        self.env = env
        self.source = source

    @property
    def report(self) -> dict:
        return {'source': self.source, 'proxies': {k: redact_url(v) for k, v in self.env.items()
                if k in PROXY_NAMES and k != 'NO_PROXY' and v},
                'no_proxy': self.env.get('NO_PROXY', ''),
                'mode': 'proxy' if any(self.env.get(k) for k in PROXY_NAMES[:3]) else 'direct'}

    @property
    def fingerprint(self) -> str:
        values = {name: self.env.get(name, '') for name in PROXY_NAMES}
        values['NO_PROXY'] = ','.join(sorted(set(values['NO_PROXY'].split(',')) - {''}))
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    def process_env(self, base=None, *, build_network=None) -> dict[str, str]:
        env = dict(os.environ if base is None else base)
        for key in ENV_NAMES:
            env.pop(key, None)
        env.update(self.env)
        # Harbor may load a model --env-file after process startup. Keep data
        # proxy interpolation pinned to the resolved config, even in that case.
        env.update({'PIPELINE_DATA_' + name: self.env.get(name, '') for name in PROXY_NAMES})
        build = self if build_network is None else build_network
        env.update({'PIPELINE_BUILD_' + name: build.env.get(name, '') for name in PROXY_NAMES})
        return env

    def with_bypass(self, hosts) -> 'NetworkConfig':
        env = self.env.copy()
        items = [v for v in (env.get('NO_PROXY', '') + ',' + DEFAULT_BYPASS).split(',') if v]
        items += [host for host in hosts if host]
        bypass = ','.join(dict.fromkeys(items))
        env.update(NO_PROXY=bypass, no_proxy=bypass)
        return NetworkConfig(env, self.source)

    def opener(self, *handlers):
        proxies = {}
        for scheme in ('http', 'https'):
            value = self.env.get(scheme.upper() + '_PROXY') or self.env.get('ALL_PROXY')
            if value:
                proxies[scheme] = value
        return urllib.request.build_opener(_ProxyHandler(proxies, self.env.get('NO_PROXY', '')), *handlers)


def resolve_network(config: dict | None = None, *, environ=None) -> NetworkConfig:
    config = {} if config is None else config
    if not isinstance(config, dict):
        raise ValueError('network must be an object')
    if config.get('enabled', True) is False:
        return NetworkConfig({}, 'disabled')
    inherited = dict(os.environ if environ is None else environ) if config.get('inherit_env', True) else {}
    supplied = read_proxy_env(config['proxy_env_file']) if config.get('proxy_env_file') else {}
    explicit = config.get('proxy_url')
    fallback = (supplied.get('PIPELINE_DATA_PROXY') or supplied.get('PROXY_FOR_TOOLS')
                or inherited.get('PIPELINE_DATA_PROXY'))
    selected = {}
    for key in PROXY_NAMES:
        # Explicit file overrides inherited aliases, including an empty value.
        mapping = supplied if key in supplied or key.lower() in supplied else inherited
        upper, lower = mapping.get(key), mapping.get(key.lower())
        overridden = (key != 'NO_PROXY' and (explicit or fallback)) or (key == 'NO_PROXY' and 'no_proxy' in config)
        if not overridden and upper and lower and upper.rstrip('/') != lower.rstrip('/'):
            raise ValueError(f'conflicting uppercase/lowercase proxy settings: {key}')
        selected[key] = upper if upper is not None else lower or ''
    if explicit:
        explicit = _validate_proxy(explicit, allow_credentials=False)
    override = explicit or fallback
    if override:
        selected.update(HTTP_PROXY=override, HTTPS_PROXY=override, ALL_PROXY='')
    source = 'proxy_url' if explicit else 'proxy_env_file' if supplied else 'environment'
    if not any(selected[k] for k in PROXY_NAMES[:3]):
        source = 'direct'
    for key in PROXY_NAMES[:3]:
        if selected[key]:
            selected[key] = _validate_proxy(selected[key])
    bypass = config.get('no_proxy', selected['NO_PROXY'] or DEFAULT_BYPASS)
    if not isinstance(bypass, str) or any(c in bypass for c in '\n\r\x00'):
        raise ValueError('no_proxy must be a comma-separated string')
    selected['NO_PROXY'] = bypass
    return NetworkConfig({alias: value for name, value in selected.items()
                          for alias in (name, name.lower())}, source)


def network_for_policy(config: dict | None, policy: str, *, environ=None, proxy_url=None) -> NetworkConfig:
    if policy not in {'enabled', 'public', 'disabled', 'no-network', 'gateway_search_only', 'allowlisted', 'allowlist'}:
        raise ValueError('unknown network policy')
    if policy in {'enabled', 'public'}:
        if proxy_url:
            config = {**(config or {}), 'proxy_url': proxy_url, 'inherit_env': False, 'enabled': True}
        return resolve_network(config, environ=environ)
    if config and (config.get('proxy_url') or config.get('proxy_env_file')):
        raise ValueError('download proxies require enabled/public policy; a proxy could bypass a host allowlist')
    return resolve_network({'enabled': False}, environ=environ)


def make_compose_network_overlay(services: dict[str, bool], network: NetworkConfig) -> dict:
    """services maps name to whether it has a build context. Values stay in env."""
    refs = {key: '${PIPELINE_DATA_' + key.upper() + ':-}' for key in ENV_NAMES}
    build_refs = {key: '${PIPELINE_BUILD_' + key.upper() + ':-}' for key in ENV_NAMES}
    result = {}
    for name, builds in services.items():
        row = {'environment': refs.copy()}
        if builds:
            row['build'] = {'args': build_refs.copy()}
        result[name] = row
    return {'services': result}


def probe_urls(spec: dict) -> list[str]:
    config = spec.get('runtime', {}).get('network', {})
    urls = config.get('probe_urls')
    if urls is None:
        data = spec.get('data', {})
        default_availability = 'runtime_download' if data.get('mode') in {'referenced', 'mixed'} else 'bundled'
        urls = [s['url'] for s in data.get('sources', [])
                if s.get('availability', default_availability) == 'runtime_download'
                and s.get('visibility', 'public') == 'public' and s.get('required', True)
                and s.get('url')]
    if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
        raise ValueError('runtime.network.probe_urls must be a list of HTTP(S) URLs')
    return list(dict.fromkeys(urls))


def container_probe_command(urls: list[str]) -> list[str]:
    # Keep a single implementation for host and container probes. This code
    # executes in a disposable container, consumes only its own proxy env, and
    # prints sanitized evidence. It does not import task-authored host code.
    source = Path(__file__).read_text().rsplit("\nif __name__ == '__main__':", 1)[0]
    source += '\nresult = preflight_network(resolve_network(), ' + repr(urls) + ')\n'
    source += "print(json.dumps(result)); raise SystemExit(0 if result['status'] == 'pass' else 2)\n"
    return ['python', '-c', source]


def preflight_network(network: NetworkConfig, urls: list[str], *, timeout=10) -> dict:
    checks = []
    if not urls:
        checks.append({'kind': 'http_target', 'status': 'fail',
                       'error': 'No HTTP(S) probe target: configure runtime.network.probe_urls or a required public runtime-download source URL; TCP reachability alone does not verify downloads'})
    proxies = sorted(set(network.env.get(k, '') for k in PROXY_NAMES[:3]) - {''})
    for proxy in proxies:
        parsed = urllib.parse.urlsplit(proxy)
        row = {'kind': 'proxy_tcp', 'endpoint': redact_url(proxy)}
        try:
            with socket.create_connection((parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)), timeout):
                pass
            row['status'] = 'pass'
        except OSError as exc:
            row.update(status='fail', error=type(exc).__name__)
        checks.append(row)
    opener = network.opener()
    for url in urls:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('preflight URLs must be HTTP(S) without embedded credentials')
        row = {'kind': 'http_head', 'endpoint': redact_url(url)}
        try:
            with opener.open(urllib.request.Request(url, method='HEAD'), timeout=timeout) as response:
                code = response.status
            row.update(status='pass' if 200 <= code < 400 else 'fail', http_status=code)
        except urllib.error.HTTPError as exc:
            row.update(status='fail', http_status=exc.code, error='HTTPError')
        except (OSError, urllib.error.URLError) as exc:
            row.update(status='fail', error=type(exc).__name__)
        checks.append(row)
    return {'schema_version': 'pipeline-network-preflight-v1', 'network': network.report,
            'checks': checks, 'status': 'pass' if checks and all(c['status'] == 'pass' for c in checks) else 'fail'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--proxy-url')
    p.add_argument('--proxy-env-file')
    p.add_argument('--no-proxy')
    p.add_argument('--direct', action='store_true')
    p.add_argument('--url', action='append', default=[])
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    config = {k: v for k, v in vars(args).items() if k in {'proxy_url', 'proxy_env_file', 'no_proxy'} and v is not None}
    if args.direct:
        config['enabled'] = False
    network = resolve_network(config)
    result = preflight_network(network, args.url) if args.url else network.report
    content = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content)
    print(content, end='')
    return 0 if result.get('status', 'pass') == 'pass' else 2


if __name__ == '__main__':
    raise SystemExit(main())
