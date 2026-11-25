"""Load the migrated implementation tree without exposing its old CLI layout."""
from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import importlib.util
import sys

IMPLEMENTATION_ROOT = Path(__file__).resolve().parent / '_implementation'
STAGE1_ROOT = IMPLEMENTATION_ROOT / 'stage1-datacard&downlond'
STAGE2_ROOT = IMPLEMENTATION_ROOT / 'stage2-TasksConstruction&Verify'
STAGE3_ROOT = IMPLEMENTATION_ROOT / 'stage3-TasksRun&Verify'


@contextmanager
def implementation_paths(*, stage: int | None = None):
    roots = [IMPLEMENTATION_ROOT]
    if stage in (1, 2, 3):
        roots.append({1: STAGE1_ROOT, 2: STAGE2_ROOT, 3: STAGE3_ROOT}[stage] / 'code')
    if stage == 2:
        roots.extend([
            Path(__file__).resolve().parent / 'stage2_construction/contracts',
            Path(__file__).resolve().parent / 'stage2_construction/validation',
            Path(__file__).resolve().parent / 'stage1_sources/materials',
            STAGE1_ROOT / 'code',
        ])
    if stage == 3:
        roots.extend([
            Path(__file__).resolve().parent / 'stage3_execution/evidence',
            Path(__file__).resolve().parent / 'stage3_execution/judge',
            Path(__file__).resolve().parent / 'stage3_execution/submission',
            Path(__file__).resolve().parent / 'stage2_construction/contracts',
            STAGE2_ROOT / 'code', STAGE1_ROOT / 'code',
        ])
    inserted=[]
    for root in reversed(roots):
        value=str(root)
        if value not in sys.path:
            sys.path.insert(0,value); inserted.append(value)
    try:
        yield
    finally:
        for value in inserted:
            try: sys.path.remove(value)
            except ValueError: pass


def load_file(path: Path, name: str, *, stage: int | None = None):
    with implementation_paths(stage=stage):
        spec=importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f'cannot load implementation module: {path}')
        module=importlib.util.module_from_spec(spec)
        sys.modules[name]=module
        spec.loader.exec_module(module)
        return module
