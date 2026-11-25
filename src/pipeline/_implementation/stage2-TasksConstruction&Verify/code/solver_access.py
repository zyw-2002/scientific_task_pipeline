"""Apply the user's online-search policy to editable task runtime and prose."""
from __future__ import annotations
import re
from pathlib import Path

POLICY = 'online_search'
PROBE_URLS = ['https://api.crossref.org/works?query=science&rows=1', 'https://www.ncbi.nlm.nih.gov/']

OFFLINE_CLAUSE = re.compile(r'\bno (?:external )?(?:network|internet) access\b(?!\s+(?:is )?required)|'
                            r'\b(?:network|internet) access (?:is )?(?:disabled|forbidden|prohibited)\b|'
                            r'\bmust (?:work|run|operate) offline\b', re.I)
LIMITED_CLAUSE = re.compile(r'\b(?:network|internet) access (?:is )?(?:restricted|limited)\b', re.I)
NO_SEARCH_CLAUSE = re.compile(r'\bno (?:external )?web search(?:es)?\b|'
                              r'\bdisabled tools?\s*:\s*[^\n.]*?\bweb(?:search|fetch)\b|'
                              r'\bwebsearch(?: and webfetch)? (?:is|are) disabled\b|'
                              r'\bweb search(?: and web fetch)?(?: tools?)? (?:are|is) disabled\b|'
                              r'\bno (?:external )?web access\b|\bno [^.!?;\n]{1,80}?\bor web access\b', re.I)
ACCESS_UNNEEDED = re.compile(r'\bno (?:external )?(?:network|internet) access (?:is )?required\b', re.I)
DENIAL = re.compile(r'\boffline\b|\bno (?:external )?(?:network|internet|web search|web access)\b|'
                    r'\bno [^.!?;\n]{1,80}?\bor web access\b|'
                    r'\b(?:forbade|prohibited|disallowed|disabled|forbidden|banned|unavailable|could not|not allowed)\b', re.I)


def access_disclosure_errors(package, spec, fixed_instruction):
    """Verify the public comparison with the original task when a Solver policy is bound."""
    if spec.get('solver_access') != POLICY:
        return []
    from contract import package_layout, package_path
    public = package_path(Path(package), package_layout(spec)['instruction_path'])
    if not public.is_file():
        return ['missing public Solver instructions']
    text = public.read_text()
    original_label = re.compile(
        r'\b(?:original|prior|previous|fixed|source)\b[^.!?;|\n]*?\b(?:benchmark|task)\b', re.I)
    access = re.compile(r'\b(network|internet|online|offline|search|websearch|webfetch|connectivity|access)\b', re.I)
    from instruction_checks import _visible_lines
    comparison = []
    for _, line in _visible_lines(text):
        label = original_label.search(line)
        if not label:
            continue
        # Only the historical side of a comparison is evidence of the original policy.
        clause = re.split(r'[.;!?]|\b(?:whereas|while|but)\s+(?:(?:in|for)\s+)?'
                          r'(?:the |this |our |we |now |current |here )|'
                          r',?\s*\b(?:this (?:run|task|version|environment)|our (?:run|solver)|the current (?:run|solver|task))\b',
                          line[label.start():], maxsplit=1, flags=re.I)[0]
        if access.search(clause):
            comparison.append(clause)
    comparison = ' '.join(comparison)
    if not comparison:
        return ['explain the original benchmark Solver access in the public instructions alongside the current online search policy']
    original = Path(fixed_instruction)
    if not original.is_file():
        return []
    source = original.read_text().partition('## Original public data description')[0].replace('**', '')
    policy_evidence = re.compile(r'\b(?:internet|online search|web ?search|web ?fetch|web access|'
                                 r'network (?:access|connectivity|is disabled|is enabled)|external network|offline)\b', re.I)
    silence = re.compile(r'\b(?:unspecified|unmentioned|silent|unclear|undocumented)\b|'
                         r"\b(?:not|never|no|didn.t|doesn.t) (?:explicit(?:ly)? )?"
                         r'(?:specif\w*|state\w*|describ\w*|mention\w*|document\w*|address\w*|'
                         r'define\w*|report\w*|network policy|access policy|search policy)\b', re.I)
    if not policy_evidence.search(source) and not silence.search(comparison):
        return ['the original benchmark gives no Solver access policy; disclose it as unspecified']
    if OFFLINE_CLAUSE.search(source) and not DENIAL.search(comparison):
        return ['the original benchmark restricts network access; disclose this original restriction next to the current Solver policy']
    if LIMITED_CLAUSE.search(source) and not re.search(r'\b(restricted|limited|only|allowlisted|allowlist)\b', comparison, re.I):
        return ['describe the original benchmark network restriction, including any permitted use']
    if NO_SEARCH_CLAUSE.search(source) and not (DENIAL.search(comparison)
                                                 or re.search(r'\bwithout (?:web |online |external )?search', comparison, re.I)):
        return ['disclose the original benchmark restriction on web search']
    if (ACCESS_UNNEEDED.search(source) and not OFFLINE_CLAUSE.search(source)
            and DENIAL.search(comparison) and not re.search(r'\b(?:required|necessary|needed|optional)\b', comparison, re.I)):
        return ['the original benchmark says network access was unnecessary; a prohibition needs separate source evidence']
    return []


def apply_policy(package, spec):
    """Mechanical access settings only; never change science, scores or data."""
    if spec.get('solver_access') != POLICY:
        return []
    runtime = spec.setdefault('runtime', {})
    if not isinstance(runtime, dict):
        return []  # Preflight reports malformed authored declarations.
    changed = []
    for key, value in [('network_policy', 'enabled'), ('search_policy', 'direct')]:
        if runtime.get(key) != value:
            runtime[key] = value
            changed.append('runtime.' + key)
    network = runtime.setdefault('network', {'inherit_env': False})
    if isinstance(network, dict) and not network.get('probe_urls'):
        network['probe_urls'] = list(PROBE_URLS)
        changed.append('runtime.network.probe_urls')
    from contract import package_layout, package_path
    layout = package_layout(spec)
    for key in ('instruction_path', 'readme_path'):
        path = package_path(Path(package), layout[key])
        if not path.is_file() or path.is_symlink():
            continue
        before = path.read_text()
        text = before
        for label, value in [('Network', 'enabled'), ('Search', 'direct')]:
            text = re.sub(r'(?m)^(\|\s*' + label + r'\s*\|)[^\n|]*(\|\s*)$',
                          lambda m: m[1] + ' ' + value + ' ' + m[2], text)
        replacements = {
            'Network access is disabled; all dependencies must be pre-installed in the container image.':
                'Network access and direct scientific searches are enabled; dependencies may be acquired within task authorization.',
            'No network access is permitted.': 'Network access and direct scientific searches are permitted.',
            'Network access: disabled (all data is provided locally)':
                'Network access: enabled; direct scientific searches are permitted (task data is provided locally)',
        }
        for old, new in replacements.items():
            # Only rewrite a current instruction, preserving quoted history and comparison rows.
            text = re.sub(r'(?m)^([ \t]*(?:[-*] )?)' + re.escape(old) + r'[ \t]*$', lambda m: m[1] + new, text)
        if text != before:
            path.write_text(text)
            changed.append(layout[key])
    return changed


def configure_compose(document, solver, policy, *, solver_only=False):
    """Give Solver an external bridge even if the old task had only internal networks."""
    service = document['services'][solver]
    owned = 'pipeline_solver_public'
    networks = document.setdefault('networks', {})
    if policy != 'enabled':
        if owned in networks:
            networks.pop(owned)
            for definition in document['services'].values():
                attached = definition.get('networks', [])
                if isinstance(attached, list):
                    definition['networks'] = [n for n in attached if n != owned]
                elif isinstance(attached, dict):
                    attached.pop(owned, None)
        return
    mode = service.pop('network_mode', None)
    attached = service.get('networks')
    if attached is None:
        # Keep the normal task-service network when it existed implicitly.
        attached = ['default'] if mode is None else []
        if mode is None:
            networks.setdefault('default', {})
    if isinstance(attached, list):
        service['networks'] = list(dict.fromkeys([*attached, owned]))
    elif isinstance(attached, dict):
        service['networks'] = dict(attached, **{owned: {}})
    else:
        raise ValueError('Solver Compose networks must be an array or object')
    networks[owned] = {'driver': 'bridge', 'internal': False}
    if solver_only:
        for name, helper in document['services'].items():
            if name == solver:
                continue
            for port in helper.get('ports', []):
                address = port.get('host_ip', '') if isinstance(port, dict) else str(port).split(':', 1)[0]
                if isinstance(port, str) and port.startswith('[::1]:'):
                    address = '::1'
                if address != '::1' and not re.fullmatch(r'127\.\d{1,3}\.\d{1,3}\.\d{1,3}', address or ''):
                    raise ValueError(f'{name}: public host port on a Solver-only network; bind the port to loopback or use service DNS')
            mode = helper.get('network_mode')
            if mode == 'none':
                continue
            if mode == 'bridge':
                helper.pop('network_mode')
            elif isinstance(mode, str) and mode.startswith('service:') and mode[8:] != solver and mode[8:] in document['services']:
                continue
            elif mode:
                raise ValueError(f'{name}: unsupported helper network_mode for a Solver-only public network: {mode}')
            for network in helper.get('networks') or ['default']:
                if network == owned:
                    raise ValueError(f'{name} cannot share the Solver public network')
                definition = networks.get(network) or {}
                if definition.get('external') or definition.get('name') or definition.get('driver', 'bridge') != 'bridge':
                    raise ValueError(f'{name}: network {network} must be a project-local bridge to enforce Solver-only internet access')
                networks[network] = {**definition, 'internal': True}
