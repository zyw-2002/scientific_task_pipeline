# Stage 01 fast construction contract

Construct one executable scientific task from the supplied paper and source bundle. This prompt is the operational contract. Read `/task/input/paths.json`, `source_inventory.json`, the paper text, fixed task instruction, and `trajectory_regressions.json` once. Work only in `paths.output_dir`.

## Required order

1. Run `python /task/input/pipeline_tools/authoring_package.py` once. Read the generated `execution.json` and the existing `common_task/evaluation/protocol.json`.
2. In the first 5 tool turns write a complete draft of `execution.json`, `instruction.md`, `method.md`, `environment_requirements.md`, `requirements.txt`, `README.md`, `data_description.md`, `data_sources.json`, `metric_adapter.py`, `protocol.json`, `calibration.json`, `scoring_smoke.json`, and fixtures. Use paper evidence from `authoring_package.py --find-quote`.
3. Run `python /task/input/pipeline_tools/preflight.py --construction --generate-framework --paths /task/input/paths.json --package /workspace/build --report /workspace/construction_preflight.json`.
4. Read only the failed check IDs and their first reason. Fix the files named by those reasons. Never print the full report, schema, source bundle, or generated files. Never rewrite a passing file. Run preflight after each coherent group of fixes.
5. Run `python /task/input/pipeline_tools/scoring_smoke.py` and the requested calibration cases. Save the JSON output. Finish with one preflight and one smoke run. Stop after at most 30 corrective tool turns. If formal resources are absent, declare them with `availability=runtime_download`, identity, schema, preparation command, and `provided=false`. Do not fabricate bytes, labels, checkpoints, or placeholder data files. A deferred resource with an authorized source, runnable `prepare.command`, and independent `prepare.validate` is a construction PASS with `execution_ready=false` until Stage3 materializes it. Mark the task `BLOCKED` only when a required resource has no authorized source or runnable, verifiable preparation path. Stage3 refers to the execution package in this reorganized pipeline; do not use host source-tree paths in the task package. Keep construction task-general: do not hardcode a familiar dataset or package (for example BBBP) as a fallback for unrelated tasks; use each task's own public source and dependency instructions.

Construction order for reliable, general packages:

- First make `execution.json` internally coherent: bind every formal input, reference, model and scored requirement to a resource ID, then make each `main_experiments` row refer only to those IDs. Do not invent experiment IDs, metric names, or paper numbers merely to fill a table.
- Use `python /task/input/pipeline_tools/authoring_package.py --find-quote "short phrase"` for every paper anchor and copy the returned exact quote, file, SHA-256 and line range. If an anchor cannot be verified, record the uncertainty in the private evidence and simplify the public claim; never invent a quotation.
- Define the evaluator and one correct, one weak-valid, and one invalid smoke case before writing `scoring_smoke.json`. Every smoke case must bind formal resources to a fixture with the same schema, and `scientific_handoff` must invoke the actual evaluator on a submission and preserve its evidence.
- Define at least one meaningful calibration mutation that is executable with an argv array and state the scientific/acceptance fault it detects. A mutation must change behavior or a metric, not just rename a file.
- Public files are Solver-visible. Do not put paper titles/citations, DOI/accession/repository/commit identifiers, source excerpts, `/task/evaluation`, `/task/input`, host paths, or original paper result claims in instruction, README, data description, manifest, or `task_contract.json`. The generated contract declares automatic private verification after submission. Only an actually mounted public validation tool may be advertised as a Solver command; the formal collector stays private.

## Hard execution rules

## Solver-visible source protection

Public instruction, README, data description, manifests, and helper scripts
must contain only the objective, schema, constraints, method requirements,
outputs, and evaluation contract. Never expose paper authors/title/journal/year,
DOI or article URLs, dataset accessions or run IDs, GitHub URLs or commit
hashes, private source-bundle paths, reference bytes, hidden labels, or claims
about the original paper's successful result. Keep source locators and result
claims in authoring/review evidence. Runtime preparation may provide an
authorized method and independent validation, but not a copyable source
address or original answer.

## Q0 task-quality gate

Before accepting the draft, make these six quality dimensions explicit in the
solver-facing documents and review evidence: objective and constraints;
bounded scope and workload; complete solvability information and authorized
resource preparation; executable evaluation rules and controls; source-backed
truthfulness and relevance; and reproducibility/fairness with leakage control.
Bind every decision to actual task evidence. A missing dimension is a
construction defect; file presence or a smoke command alone is not a quality
pass. Do not use a familiar fallback dataset to hide missing inputs.

Use the authoring scaffold as the base and edit it in place. A valid direct-file
submission has this exact shape before framework generation:

```json
{"submission":{"path":"/workspace/output/result.ext",
 "required_artifacts":[{"id":"result","description":"The scored result",
 "format":"text","paths":["{submission}"]}]}}
```

Every `data.sources[]` entry needs `id`, `name`, `package_path` (package-relative),
`role` (`input`, `reference`, `fixture`, or `support`), `visibility`, `origin`,
`availability`, string `identity`, `version`, `content.format`, and a real
`schema_description`. A deferred formal input also needs `prepare.command`,
`prepare.validate`, `prepare.access`, and positive `prepare.timeout_sec`.
Use `common_task/environment/input/data/<name>` for public fixtures. The
Solver sees that location as `/task/problem/data/<name>`. Source archives and
paper/code files are build evidence with `role=support`, `visibility=build_only`,
and must not be required Solver inputs. Do not use `/task/problem/...` as a
`package_path`.

Do not replace `scoring_smoke.json` with an ad-hoc schema. It must retain
`schema_version=pipeline-scoring-smoke-v1`, use relative fixture paths under
`common_task/evaluation`, set `expected_status=success` for valid controls and
`invalid_submission` for rejected controls, and bind every case to the
requirements it tests. The invalid control must include
`expected_error_contains`. A correct and weak-valid control must provide
independent metric or reward expectations for the actual protocol metric names.

- Do not add unknown top-level keys to `execution.json`. Allowed top-level keys are the ones in `/task/input/pipeline_tools/execution.schema.json`. Put scientific prose in `instruction.md`, never in `execution.results_json`, `execution.output_files`, or custom keys.
- Do not hand-write `task.toml`, Compose, `test.sh`, `task_contract.json`, evaluator entrypoints, or generated tables. Framework generation owns them.
- Preserve the initial execution scaffold's integer `runtime.cpu`, `runtime.memory_mb`, `runtime.gpus`, and `runtime.time_limit_sec` values. Do not delete them or set them to null. `execution.json.submission.path` is one absolute `/workspace/output/<filename>` path. `submission.required_artifacts` is an array of objects with `id`, `description`, `format`, and exactly one `paths` or `pattern` declaration. Put `paths: ["{submission}"]` only inside the single direct-file artifact object. Never add `submission.paths`, string-only artifacts, `submission_json`, or any other submission-level path field. Keep `data.sources` as an array, including when the mode is `referenced` or `mixed`.
- Every formal source has `id`, `package_path`, `role`, `availability`, `identity`, `version`, `schema_description`, and a runnable Stage3 preparation recipe in the execution package. Use `/task/problem/data` as `DATA_DIR` and `/workspace/output` as `OUTPUT_DIR`.
- The only legal `availability` values are `bundled`, `preprovided`, `runtime_download`, `unverified`, and `unavailable`. Never write `available`. The article, source-code archive, and authoring metadata under `/task/input/source_bundle` are authoring evidence, not Solver data. Do not list them as required `data.sources` unless you actually copy a small fixture into the package. Formal reference/query data, labels, checkpoints, and splits need their own stable IDs, schemas, and Stage3 preparation recipes. Use `availability=runtime_download` or `preprovided` with `provided=false` when the full bytes are deferred. A deferred resource with an authorized source, runnable `prepare.command`, and independent `prepare.validate` is a construction PASS with `execution_ready=false` until Stage3 materializes it. Mark the task `BLOCKED` only when a required resource has no authorized source or runnable, verifiable preparation path.
- Public formal resource paths are package-relative paths such as `common_task/environment/input/data/reference.h5ad` and `common_task/environment/input/data/query.h5ad`, which map to `/task/problem/data/...` for the Solver. A deferred recipe must read the complete Stage3 mount `/task/problem/data`, copy or validate into `{output}`, and record the real schema and split. Do not use `files/...` from the authoring source bundle as a Solver data path.
- `protocol.json` must bind every main paper experiment to a quality requirement, formal resource IDs, metric unit, population, split, deliverable, aggregation, source evidence, and objective. Use one stable experiment ID per experiment. `scope.figure_table_decisions` covers every paper caption.
- Use the evaluator ABI exactly. Each `units[]` item has `id`, `path` (use `{submission}` for a direct single-file submission), and the metric name. Each `aggregations[]` item has `output`, `operation` (`identity`, `mean`, `std`, `sum`, `min`, or `max`), `metric`, and `unit_ids`. `reward_metric` must equal an aggregation `output`, and `reward_direction` must be `higher_is_better` or `lower_is_better`. Do not substitute `unit_id`, `aggregation_id`, or `method` for these fields.
- For every `main_experiments` entry, `resource_ids` must point to the formal dataset/checkpoint IDs used by that experiment, `requirement_ids` must point to existing requirements, and `unit_ids` must point to existing evaluator units. Do not use article or code IDs as experiment data. Cite exact paper paths and line/caption evidence for objective, scale, split, and metric. Set `assessment.scope.scope_policy=full` when reproducing the complete paper panel. Set `scope_policy=bounded` for a deliberate source-backed subset, and document `task_scope`, `difficulty_impact`, `assessment.adaptations`, and source evidence; this choice is part of the generated package contract and never requires an interactive case decision.
- Add `assessment.scope.difficulty_profile` with `version=1`, source evidence, six 1–5 level dimensions for both paper and task, `main_experiment_count`, positive weights, `overall_gap_limit <= 0.20`, and `split_drop_limit <= 1`. The paper side records independent-reference and model/checkpoint requirements. The task side records the corresponding evaluator-bound resources and method type. Preflight rejects an overall gap above 20%, a split drop above one level, an undocumented bounded scope, self-reported scoring in place of independent reference data, or a rule-only task where a model/checkpoint is required.
- Set `difficulty_contract_version=1` in `execution.json`; without this marker the profile is not eligible for production handoff.
- Implement real scientific scoring in `metric_adapter.py`. It must use independent references or stated analytical/objective rules and never read submitted self-reported scores. `scoring_smoke.json` must include one correct, one weak-valid, and one invalid submission with independently computed expected ranges. Missing units count in the declared denominator.
- Each smoke case has a unique nonempty `id`, `kind` (`correct`, `weak_valid`, or `invalid`), `expected_status`, `description`, and a submission fixture. Valid cases declare `expected_metrics` or `expected_reward`/bounds for the actual aggregation output. Invalid cases declare no score and test a specific rejection.
- `instruction.md` must contain `## Environment`, `## Inputs`, `## Output`, and `## Evaluation`, with fixed paths, purpose, formats, fields, required/optional status, evaluator argv, success condition, baseline-first procedure, experiment log fields, scientific restrictions, and this run's online-search difference from the original benchmark. Use English for new prose.
- Public package documentation is mounted at `/task/problem/README.md` and `/task/problem/data_description.md`. Reference those exact container paths in the Input section when they appear in `task_contract.json`. Do not write `data/README.md`, a host path under `/u01/`, or any private evaluator path as a Solver input.
- Public validation may be omitted when no Solver-visible development set and runnable validation script are supplied. Never reference `/task/evaluation` or private labels from public commands.
- Construction uses small fixtures only. Formal data, checkpoint, split, and full-scale run are Stage3 execution responsibilities. A missing key model/label/checkpoint makes status `BLOCKED`. A repairable path/evaluator issue is `REWORK`.

## Final check

Open only the final `execution.json`, generated `common_task/task_contract.json`, and `common_task/instruction.md`. Verify that the generated contract, instruction, test, and evaluator use the same paths. Run the real smoke and leave `construction_preflight.json`, `scoring_smoke.json`, and all cited paper evidence in the output. Do not claim formal data execution from a fixture.


## Shipped tool entrypoints

The following no-argument launches are available for the tool smoke check. Use the fully parameterized commands in the required order above for the actual package.

```bash
python /task/input/pipeline_tools/authoring_package.py
python /task/input/pipeline_tools/preflight.py
python /task/input/pipeline_tools/scoring_smoke.py
python /task/input/pipeline_tools/review_bindings.py --reviewed
```

## Source-binding checklist before handoff

Copy the complete main-experiment inventory from `authoring_package.py` when `scope_policy=full`. For `scope_policy=bounded`, declare the selected source-backed experiments and record the omitted experiments in the task scope/adaptations with source evidence. Every declared entry must contain `statement`, `population`, `split`, `deliverable`, `metric_unit`, `aggregation`, `resource_ids`, `requirement_ids`, `unit_ids`, `origin`, and exact `source_evidence`. `resource_ids` must be formal dataset, query, label, split, or checkpoint IDs used by that experiment. `requirement_ids` and `unit_ids` must resolve to existing quality requirements and evaluator units.

Fill the four `assessment.scope.source_claims` entries (`objective`, `scale`, `split`, `metric`) with exact quote, frozen source path, hash, and locator objects. Fill `scope.figure_table_decisions` for every caption returned by `authoring_package.py` with `covered`, `duplicate`, `schematic`, or `outside_scope`, a source-bound rationale, and the required experiment or boundary evidence. Never omit a caption. Use the exact objects returned by `authoring_package.py --find-quote`.

Before the final preflight, mechanically confirm that every binding resolves, every formal resource is covered by a main experiment, every evaluator unit is aggregated, and every caption has a decision. Missing source evidence or an unresolved binding requires repair or `BLOCKED` status before smoke evidence is accepted.

Compatibility path for frozen role snapshots is `/task/input/fixed_task_instruction.md`. The construction tool invocation is also available as `python /task/input/pipeline_tools/preflight.py` followed by the required flags above.

python /task/input/pipeline_tools/preflight.py
