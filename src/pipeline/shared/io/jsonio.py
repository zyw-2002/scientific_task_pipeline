"""Strict JSON/JSONL and atomic file helpers shared by all pipeline stages."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Iterable


def _reject_constant(value: str):
    raise ValueError(f"invalid JSON constant: {value}")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"),
                      object_pairs_hook=_unique_pairs,
                      parse_constant=_reject_constant)


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    result = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line, object_pairs_hook=_unique_pairs,
                           parse_constant=_reject_constant)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{number}: expected a JSON object")
        result.append(value)
    return result


def atomic_write_text(path: str | Path, text: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(target)
    return target


def atomic_write_json(path: str | Path, value: Any) -> Path:
    return atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False,
                                               allow_nan=False) + "\n")


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> Path:
    text = "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in rows)
    return atomic_write_text(path, text)
