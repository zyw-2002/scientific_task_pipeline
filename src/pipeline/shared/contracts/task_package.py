"""Stable cross-stage task-package boundary.

Stage2 owns the scientific implementation; Stage3 consumes this small boundary.
The module deliberately validates identity/readiness without importing task code.
"""
from __future__ import annotations
from pathlib import Path
from typing import Any
from pipeline.shared.identity.hashing import sha256_file
from pipeline.shared.io.jsonio import read_json

REQUIRED_ROOT_FILES = ("execution.json", "readiness.json")


def load_package(package: str | Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    root = Path(package).resolve()
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"task package is not a directory: {package}")
    missing = [name for name in REQUIRED_ROOT_FILES if not (root / name).is_file()]
    if missing:
        raise ValueError(f"task package is missing: {', '.join(missing)}")
    execution = read_json(root / "execution.json")
    readiness = read_json(root / "readiness.json")
    if not isinstance(execution, dict) or not isinstance(readiness, dict):
        raise ValueError("execution.json and readiness.json must contain objects")
    return root, execution, readiness


def package_identity(package: str | Path) -> dict[str, Any]:
    root, execution, readiness = load_package(package)
    return {
        "task_id": execution.get("task_id"),
        "canonical_id": execution.get("canonical_id"),
        "package_sha256": readiness.get("package_sha256"),
        "execution_sha256": readiness.get("execution_sha256"),
        "readiness_scope": readiness.get("readiness_scope"),
        "construction_ready": readiness.get("construction_ready", readiness.get("run_ready")),
        "execution_ready": readiness.get("execution_ready", readiness.get("run_ready")),
    }


def require_construction_ready(package: str | Path) -> dict[str, Any]:
    identity = package_identity(package)
    if identity["construction_ready"] is not True:
        raise ValueError("task package is not construction-ready")
    return identity
