"""Stable identity and content hashing helpers."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from typing import Any


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest(path: str | Path) -> str:
    return sha256_file(path)


def fingerprint(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def slug(value: Any, *, limit: int = 65) -> str:
    text = str(value)
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", text).strip(".-")[:limit] or "item"
    return safe + "-" + hashlib.sha256(text.encode()).hexdigest()[:10]
