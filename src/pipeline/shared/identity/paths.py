"""Path validation used at stage boundaries."""
from __future__ import annotations
from pathlib import Path


def require_absolute(path: str | Path, *, label: str = "path") -> Path:
    value = Path(path)
    if not value.is_absolute() or ".." in value.parts:
        raise ValueError(f"{label} must be an absolute path without '..': {path}")
    return value


def ensure_separate(output: str | Path, *inputs: str | Path | None) -> None:
    target = Path(output).resolve()
    for item in inputs:
        if item is None:
            continue
        source = Path(item).resolve()
        if target == source or target.is_relative_to(source) or source.is_relative_to(target):
            raise ValueError(f"output overlaps input: {target} / {source}")
