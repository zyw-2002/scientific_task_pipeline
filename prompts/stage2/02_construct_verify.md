# Stage 02 fast independent construction review

Review the package in `paths.output_dir` independently of the Stage 01 worker. Read `paths.json`, the paper/source inventory, the fixed task instruction, `execution.json`, public instruction/README/data description, generated `task_contract.json`, `protocol.json`, evaluator, smoke evidence, and the Stage 01 preflight report. Work only in the assigned output directory.

## Review order

1. Run `python /task/input/pipeline_tools/preflight.py --construction --generate-framework --review-report --paths /task/input/paths.json --package /workspace/verified --report /workspace/review_preflight.json`.
2. Read only failed check IDs and concise reasons. Repair a path or evaluator mismatch in the draft when the scientific task remains valid. Re-run preflight, then run `python /task/input/pipeline_tools/scoring_smoke.py` and calibration. Do not rewrite generated files. If a key formal model, label, target, or checkpoint has no authorized source or runnable validation recipe, mark `BLOCKED`. A declared runtime download/preprovided resource with an independent validation recipe remains `PASS` for construction and `execution_ready=false` until Stage3 preparation. Mark `REWORK` for a repairable contract/evaluator issue. Mark `PASS` only when all required checks and real metric controls pass.
3. Write `review_report.json` with status, canonical/task IDs, paper scope, experiment/resource/requirement bindings, all check results, smoke/calibration evidence, and explicit reasons for REWORK/BLOCKED. Preserve source hashes and line citations.

## Mandatory gates

The review must explicitly assess the six Q0 task-quality dimensions:
objective/constraints, scope/workload, solvability information, evaluability,
source-backed relevance, and reproducibility/fairness/leakage. Cite current
package evidence and frozen source evidence for each. Missing dimensions,
unbounded scope, unverifiable metrics, or undocumented resource assumptions
keep the package in REWORK or BLOCKED.

- Solver contract exists at `common_task/environment/input/task_contract.json`. Every input and output has a fixed absolute Solver path, purpose, format, fields/schema, `required`, and data responsibility. Contract runtime has `DATA_DIR=/task/problem/data`, `OUTPUT_DIR=/workspace/output`, working directory, and an entrypoint such as `python /workspace/run.py`. `evaluation.command` is the actual evaluator argv and its success condition refers to real finite metrics.
- Public package documentation is mounted at `/task/problem/README.md` and `/task/problem/data_description.md`. Verify those exact paths are listed in instruction.md when present in the contract. A host path under `/u01/`, `data/README.md`, or a private evaluator path is invalid as a Solver input.
- Instruction, README, data description, contract, Compose, `test.sh`, verifier, and evaluator agree on paths and filenames. Every declared output is checked by the verifier. Every verifier-read output is declared. Public documents never reference private `/task/evaluation` or hidden labels.
- Formal resources are bound to real paper experiments and are either delivered with nonempty validated bytes or explicitly deferred with identity, version, schema, split, preparation command, and Stage3 runtime validation. Empty/placeholder HDF5/H5AD/ZIP files and invented model/checkpoint paths are failures.
- Check the evaluator ABI exactly. `units[]` uses `id` and `path`, direct single-file submissions use `{submission}`, and `aggregations[]` uses `output`, `operation`, `metric`, and `unit_ids`. `reward_metric` must name an aggregation output. Treat aliases such as `unit_id`, `aggregation_id`, and `method` as repairable schema errors only when the mapping is unambiguous.
- The task covers the paper's complete main experiments when `scope_policy=full`. A source-backed subset may use `scope_policy=bounded` with explicit task scope, adaptations, difficulty impact, and source evidence. Each declared experiment has statement, population, split, deliverable, metric unit, aggregation, formal resource IDs, quality requirement IDs, and source evidence. Every paper figure/table has a `covered`, `duplicate`, `schematic`, or `outside_scope` decision with caption evidence.
- Check `assessment.scope.difficulty_profile` and record its computed distance and scope policy in `review_report.json`. Enforce the 20% overall gap and one-level split-drop limits, reject undocumented scope reductions, preserve independent evaluator reference data, and preserve any required model/checkpoint. A self-reported score or rule-only replacement is a failed alignment check.
- The evaluator performs a scientific metric calculation using independent reference/analytical/objective data. JSON shape or constant checks alone fail. Smoke must distinguish correct and weak-valid submissions, reject invalid/empty submissions, and test fixed denominators, missing units, equivalent packaging, and the principal calibration control.
- Solver access is declared. This run permits online scientific search, and the public instruction records the difference from the original offline benchmark. Stage 01 uses fixtures only. Full data/checkpoint acquisition and actual split/file reads are Stage3 execution responsibilities.
- `review_report.json` classifies the task as exactly `PASS`, `REWORK`, or `BLOCKED`. `PASS` means executable after formal resource preparation and all static/metric checks pass. Use `task_contract_fail`, `agent_capability_fail`, `infrastructure_fail`, or `scientific_validity_fail` only for Stage3 runtime/Judge attribution, not for design status.


## Shipped tool entrypoints

The following no-argument launches are available for the tool smoke check. Use the fully parameterized commands in the required order above for the actual package.

```bash
python /task/input/pipeline_tools/authoring_package.py
python /task/input/pipeline_tools/preflight.py
python /task/input/pipeline_tools/scoring_smoke.py
python /task/input/pipeline_tools/review_bindings.py --reviewed
```

## Output

Return the final package plus `review_report.json`, the preflight report, real scoring smoke/calibration results, and paper evidence. Do not claim a formal dataset or checkpoint was executed when only a fixture was available.

For source fidelity, compare `assessment.scope.main_experiments` with the full inventory from `authoring_package.py`, then verify every experiment's resource, requirement, unit, and quote binding. Check all four `source_claims` dimensions and every `figure_table_decisions` entry. A missing quote, unresolved ID, unaggregated unit, or unaccounted caption is a construction defect and must remain `REWORK` or `BLOCKED` until repaired. Do not award `PASS` on a package that only has syntactically valid JSON or a constant smoke score.

Compatibility path for frozen role snapshots is `/task/input/fixed_task_instruction.md`. The review tool invocation is also available as `python /task/input/pipeline_tools/preflight.py` followed by the required flags above.
