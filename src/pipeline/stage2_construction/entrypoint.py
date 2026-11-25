"""Stage2 CLI facade; implementation details live below the package boundary."""
from __future__ import annotations
from pipeline._legacy_loader import STAGE2_ROOT, load_file, implementation_paths


def main(argv=None) -> int:
    module=load_file(STAGE2_ROOT/'code/pipeline.py', '_stage2_pipeline_impl', stage=2)
    with implementation_paths(stage=2):
        return module.main(argv)
