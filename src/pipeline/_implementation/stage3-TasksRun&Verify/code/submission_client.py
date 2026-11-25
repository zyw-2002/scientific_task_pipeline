#!/usr/bin/env python3
"""Public mailbox client: checks are repeatable; submission is final."""
import argparse
import json
import os
from pathlib import Path
import time
import uuid


def request(action, root=Path('/task/submission_control'), timeout=120, request_id=None):
    rid = request_id or uuid.uuid4().hex
    if not rid.isalnum() or len(rid) > 64:
        raise ValueError('request ID must be 1..64 alphanumeric characters')
    path = root / 'outbox' / (rid + '.json')
    payload = {'request_id': rid, 'action': action}
    if path.exists():
        if json.loads(path.read_text()) != payload:
            raise ValueError('request ID already used for another action')
    else:
        temporary = path.with_suffix('.tmp-' + uuid.uuid4().hex)
        temporary.write_text(json.dumps(payload))
        os.replace(temporary, path)
    deadline = time.monotonic() + timeout
    response = root / 'inbox' / (rid + '.json')
    while time.monotonic() < deadline:
        if response.is_file():
            return json.loads(response.read_text())
        time.sleep(0.1)
    return {'status': 'timeout', 'request_id': rid,
            'reason': 'No acknowledgement. Retry with this same --request-id; do not assume submission succeeded.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'submit'])
    parser.add_argument('--root', type=Path, default=Path('/task/submission_control'))
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--request-id')
    args = parser.parse_args()
    result = request(args.action, args.root, args.timeout, args.request_id)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result['status'] in {'pass', 'accepted'} else 1)
