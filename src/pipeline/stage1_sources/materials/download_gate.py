"""Per-resource admission checks between discovery and material transfer."""
from copy import deepcopy
import re
import urllib.parse


def valid_url(url):
    if not isinstance(url, str) or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        parsed.port
        return bool(parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username
                    and not parsed.password and not re.search(r'/goto(?:/|$)', parsed.path, re.IGNORECASE))
    except ValueError:
        return False


def resource_plan(records, api):
    """Collect resources independently; missing code never blocks obtaining a paper."""
    plan = []
    seen = set()
    def item_for(record, kind, url):
        if valid_url(url):
            return api.url_plan_item(record, kind, url, required=False)
        return {'canonical_id': record['canonical_id'], 'kind': kind, 'input_url': url,
                'resource_id': api.resource_id(record['canonical_id'], kind, url),
                'action': 'download', 'resolver': 'invalid', 'required_for_gate': False}
    for record in records:
        canonical = record.get('canonical_id')
        if not isinstance(canonical, str) or not canonical or canonical != api.safe_name(canonical, '') or canonical in seen:
            raise ValueError('papers require unique safe canonical_id values')
        seen.add(canonical)
        content = record.get('paper_content') or {}
        local = api.local_paper_item(record) if content.get('paper_available') and content.get('status') in api.PAPER_AVAILABLE_STATUSES else None
        if local:
            plan.append(local)
        else:
            doc = record.get('document') or {}
            for url in api.unique([doc.get('pdf_url'), *(doc.get('pdf_urls') or [])]):
                if isinstance(url, str):
                    plan.append(item_for(record, 'paper', url))
                    break
        resources = record.get('resources') or {}
        for kind, field in api.RESOURCE_FIELDS.items():
            for url in api.unique(resources.get(field) or []):
                if isinstance(url, str):
                    plan.append(item_for(record, kind, url))
    return plan


def filter_plan(plan, records, api):
    index = {record['canonical_id']: record for record in records}
    ready, checks, seen = [], [], set()
    for original in plan:
        item = deepcopy(original)
        record = index[item['canonical_id']]
        kind, url = item['kind'], item.get('input_url')
        decision, reason = 'ready', 'upstream_accepted_resource'
        if item.get('action') == 'copy_local_artifacts':
            reason = 'upstream_validated_local_paper; local_bytes_checked_during_materialization'
        elif not valid_url(url):
            decision, reason = 'rejected', 'invalid_absolute_url_or_redirect_wrapper'
        elif kind != 'paper' and api.status_value(record, kind) != 'found':
            decision, reason = 'needs_review', 'resource_not_marked_found_by_upstream_validation'
        if decision == 'ready' and url:
            parsed = urllib.parse.urlsplit(url)
            url = urllib.parse.urlunsplit(parsed._replace(fragment=''))
            item['input_url'] = url
            item['resource_id'] = api.resource_id(item['canonical_id'], kind, url)
        marker = item['resource_id']
        if decision == 'ready' and marker in seen:
            decision, reason = 'duplicate', 'same_paper_kind_url_already_queued'
        check = {'canonical_id': item['canonical_id'], 'resource_id': marker, 'kind': kind,
                 'url': url, 'decision': decision, 'reason': reason,
                 'network_checked': False, 'content_check': 'performed_during_download'}
        checks.append(check)
        if decision == 'ready':
            item['download_gate'] = check
            ready.append(item)
            seen.add(marker)
    return ready, checks
