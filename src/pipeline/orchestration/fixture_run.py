"""Run the complete offline Stage1 -> Stage2 -> Stage3 contract fixture."""
from __future__ import annotations
from pathlib import Path
import json
import shutil
from pipeline.shared.io.jsonio import read_jsonl
from pipeline.stage1_sources.contracts import validate_records
from pipeline.stage2_construction.workflow.test_harness import run_harbor_test
from pipeline.stage3_execution.runtime.fake_harbor import run_fake_solver


def run_fixture_case(root: str | Path) -> dict:
    root=Path(root).resolve()
    fixture=Path(__file__).resolve().parents[3] / 'tests/fixtures'
    if root.exists(): shutil.rmtree(root)
    root.mkdir(parents=True)
    stage1_errors=[]
    for rel,boundary in [('stage1/input/01_unified_sources.jsonl','unified'),
                         ('stage1/expected/08_serpapi_enriched.jsonl','searched'),
                         ('stage1/expected/10_download_results.jsonl','materials')]:
        stage1_errors.extend(validate_records(read_jsonl(fixture/rel), boundary))
    stage1={'status':'pass' if not stage1_errors else 'fail','errors':stage1_errors,
            'records':3,'fixture_root':str(fixture/'stage1')}
    (root/'stage1_report.json').write_text(json.dumps(stage1,indent=2)+'\n')
    stage2=run_harbor_test(draft=fixture/'stage2/draft', output_root=root/'stage2')
    stage3=run_fake_solver(package=fixture/'stage3/task_package', output_root=root/'stage3', solver=_write_prediction)
    result={'schema_version':'pipeline-fixture-run-v1','status':'pass' if stage1['status']=='pass' and stage2['status']=='ready' and stage3['status']=='passed' else 'fail',
            'stage1':stage1,'stage2':stage2,'stage3':stage3}
    (root/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def _write_prediction(public: Path, output: Path) -> None:
    import csv
    with (public/'test.csv').open(newline='',encoding='utf-8') as stream:
        rows=list(csv.DictReader(stream))
    with (output/'predictions.csv').open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=['sample_id','prediction']); writer.writeheader()
        for row in rows: writer.writerow({'sample_id':row['sample_id'],'prediction':'1.5'})


def run_source_case(*, root: str | Path, source_bundle: str | Path) -> dict:
    """Run the offline flow using a real frozen Stage1 source bundle."""
    root=Path(root).resolve(); source=Path(source_bundle).resolve()
    fixture=Path(__file__).resolve().parents[3] / 'tests/fixtures'
    if root.exists(): shutil.rmtree(root)
    root.mkdir(parents=True)
    manifest=json.loads((source/'materials_manifest.json').read_text(encoding='utf-8'))
    available=[item for item in manifest.get('artifacts',[]) if item.get('status')=='available']
    missing=[item.get('snapshot_path') for item in available if not (source/item.get('snapshot_path','')).is_file()]
    stage1={'status':'pass' if not missing else 'fail', 'canonical_id':manifest.get('canonical_id'),
            'schema_version':manifest.get('schema_version'), 'available_artifacts':len(available),
            'missing_artifacts':missing, 'source_bundle':str(source)}
    (root/'stage1_report.json').write_text(json.dumps(stage1,indent=2)+'\n')
    draft=root/'stage2_draft'; shutil.copytree(fixture/'stage2/draft', draft)
    shutil.copy2(source/'materials_manifest.json', draft/'materials_manifest.json')
    stage2=run_harbor_test(draft=draft, output_root=root/'stage2')
    stage3=run_fake_solver(package=fixture/'stage3/task_package', output_root=root/'stage3', solver=_write_prediction)
    result={'schema_version':'pipeline-source-case-v1',
            'status':'pass' if stage1['status']=='pass' and stage2['status']=='ready' and stage3['status']=='passed' else 'fail',
            'stage1':stage1,'stage2':stage2,'stage3':stage3}
    (root/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result
