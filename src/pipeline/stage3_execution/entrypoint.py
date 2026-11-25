"""Stage3 CLI facade for task preparation, execution, freeze and Judge."""
from __future__ import annotations
from pipeline._legacy_loader import STAGE3_ROOT, load_file, implementation_paths


def pipeline_main(argv=None) -> int:
    module=load_file(STAGE3_ROOT/'code/pipeline.py', '_stage3_pipeline_impl', stage=3)
    with implementation_paths(stage=3):
        return module.main(argv)


def workflow_main(argv=None) -> int:
    module=load_file(STAGE3_ROOT/'code/workflow.py', '_stage3_workflow_impl', stage=3)
    with implementation_paths(stage=3):
        return module.main(argv)
