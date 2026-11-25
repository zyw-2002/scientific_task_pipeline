"""Stage1 source-flow entrypoint for the reorganized package."""
from __future__ import annotations
from pathlib import Path
import runpy
import sys
from pipeline._legacy_loader import STAGE1_ROOT, implementation_paths

STEP_FILES = {
    'unify': '01_unify_sources.py',
    'deduplicate': '02_deduplicate_and_merge.py',
    'sample': '03_filter_reviews_and_sample.py',
    'collect-content': '04_collect_article_content.py',
    'extract-candidates': '05_extract_resource_candidates.py',
    'validate-candidates': '06_validate_and_merge_resources.py',
    'enrich': '07_open_source_enrichment.py',
    'search': '08_serpapi_fallback.py',
    'quality': '09_check_pipeline_results.py',
    'download': '10_plan_and_download_materials.py',
    'handoff': '11_export_pipeline_handoff.py',
}


def run_step(step: str, argv: list[str] | None = None) -> int:
    try: filename=STEP_FILES[step]
    except KeyError as exc: raise ValueError(f'unknown Stage1 step: {step}') from exc
    script=STAGE1_ROOT/'code'/filename
    if not script.is_file(): raise FileNotFoundError(script)
    old_argv=sys.argv
    sys.argv=[str(script), *(argv or [])]
    try:
        with implementation_paths(stage=1):
            runpy.run_path(str(script), run_name='__main__')
    finally:
        sys.argv=old_argv
    return 0
