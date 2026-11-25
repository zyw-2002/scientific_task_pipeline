# scientific_task_pipeline

This project reorganizes the production Stage1 -> Stage2 -> Stage3 flow from the original pipeline. The old repository is not modified by this project.

## Layout

- `src/pipeline/shared`: stable cross-stage contracts and infrastructure.
- `src/pipeline/stage1_sources`: source preparation, historical JSON/JSONL boundaries, material downloads and paper provenance.
- `src/pipeline/stage2_construction`: authoring, Harbor construction, contracts, validation, review, preflight and readiness.
- `src/pipeline/stage3_execution`: task preparation, Solver execution, submission evidence, runtime checks and Judge.
- `src/pipeline/orchestration`: the top-level dispatcher.
- `inputs/catalog` and `inputs/manifests`: small, reviewable inputs that may be tracked.
- `inputs/raw`, `inputs/downloaded`, `work`, `packages`, `runs`, and `logs`: generated or private material ignored by Git.
- `tests/fixtures`: two redacted Stage1 records and minimal Stage2/Stage3 contract fixtures; no historical results are copied.

The remaining implementation snapshot is kept under `src/pipeline/_implementation` only for modules that have not yet been split. Contracts, validation, material helpers, evidence, submission rules and runtime checks already import from the formal `pipeline.*` subpackages. The snapshot contains source code, schemas, prompts and references only; it does not contain Stage1 output, experiment results, logs or downloaded materials.

## Entry points

```bash
./bin/run_pipeline.sh plan --root "$PWD"
./bin/stage1 --help
./bin/stage2 --help
./bin/stage3 --help
```

Stage2 and Stage3 accept the original container boundary (`/task/input`, `/task/problem`, `/workspace`) while their host-side implementation is imported through `pipeline.*`. The Stage2 test harness and Stage3 fake Solver use local files and never start Docker or call a model endpoint.

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -v
```

The tests cover historical Stage1 field presence, Stage2 construct/verify Harbor result manifests, Stage3 Solver input/output binding, and prompt path boundaries. Real Harbor execution remains an explicit integration run configured through `config/stage2-worker.json.example`; it is not required for the deterministic test suite.

## Ten-task iteration protocol

`bin/benchmark_iteration` implements the development loop for the fixed 100-task OriginBench seed pool. Every iteration samples exactly ten task IDs uniformly without replacement from the complete pool, using a recorded seed and pool hash:

```bash
./bin/benchmark_iteration select --pool /path/to/selected100.jsonl \
  --output runs/iterations/iteration-001/selection.json --seed 20261125
./bin/benchmark_iteration preflight \
  --selection runs/iterations/iteration-001/selection.json \
  --output runs/iterations/iteration-001/preflight.json
```

Preflight runs after selection and records `ready`, `downloadable`, `partial`, or `blocked`. An empty data directory never becomes a scientific fixture by default. Public downloads and package-specific installation are allowed only when declared by that task; unrelated fallback datasets, synthetic replacements, and self-reported scores are not accepted. Missing material remains unscored (`null`) with its evidence and error recorded.

The preserved rubric is summarized with `benchmark_iteration score`. A batch passes only when all ten tasks have valid rubric scores and the mean is at least 90; the report also includes scoreable coverage and blocked/unscored counts. Existing baseline and candidate results must be compared on the same selected task IDs before merging a pipeline revision.
