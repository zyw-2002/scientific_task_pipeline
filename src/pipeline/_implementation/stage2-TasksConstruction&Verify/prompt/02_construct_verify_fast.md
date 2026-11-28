# Stage 02 fast independent construction review

Review the package in `paths.output_dir` independently of the Stage 01 worker. Read `paths.json`, the paper/source inventory, the fixed task instruction, `execution.json`, public instruction/README/data description, generated `task_contract.json`, `protocol.json`, evaluator, smoke evidence, and the Stage 01 preflight report. Work only in the assigned output directory.

## Review order

1. Run `python /task/input/pipeline_tools/preflight.py --construction --generate-framework --review-report --paths /task/input/paths.json --package /workspace/verified --report /workspace/review_preflight.json`.
2. Read only failed check IDs and concise reasons. Repair a path or evaluator mismatch in the draft when the scientific task remains valid. Re-run preflight, then run `python /task/input/pipeline_tools/scoring_smoke.py` and calibration. Do not rewrite generated files. If a key formal model, label, target, or checkpoint has no authorized source or runnable validation recipe, mark `BLOCKED`. A declared runtime download/preprovided resource with an independent validation recipe remains `PASS` for construction and `execution_ready=false` until Stage3 preparation. Mark `REWORK` for a repairable contract/evaluator issue. Mark `PASS` only when all required checks and real metric controls pass.

Repair in this order: (1) fix `execution.json` resource, requirement and experiment bindings; (2) fix the authored evaluator, calibration and smoke configuration; (3) let framework generation refresh `task_contract.json`, Compose, tests and public tables; (4) rerun smoke/calibration and preflight. Never hand-edit generated files. The public contract must not contain `/task/evaluation`, `/task/input`, host paths, source locators, or original paper result claims; the public contract declares automatic private verification and only exposes genuinely callable public validation tools. A failed paper quote must be replaced with an exact `authoring_package.py --find-quote` result or removed from the public claim, not guessed.
3. Run `python /task/input/pipeline_tools/authoring_package.py --review-template` to initialize the report without overwriting any findings. Write `review_report.json` with status, canonical/task IDs, paper scope, experiment/resource/requirement bindings, all check results, smoke/calibration evidence, and explicit reasons for REWORK/BLOCKED. Preserve source hashes and line citations.

## Mandatory gates

Before accepting the package, scan every Solver-visible Markdown/JSON/YAML/TOML
file for source locators and answer leakage. Reject DOI/article URLs,
paper-identifying author/title/journal/year combinations, PRJ/ERR/SRR/GEO
accessions, GitHub URLs and commit hashes, private source-bundle paths, and
original success claims such as “fully recovered”. Such evidence belongs in
the private review report only. A public runtime preparation recipe may state
how to obtain and validate an authorized resource without exposing the
original source address or reference answer.

The review must explicitly assess the six Q0 task-quality dimensions:
objective/constraints, scope/workload, solvability information, evaluability,
source-backed relevance, and reproducibility/fairness/leakage. Cite current
package evidence and frozen source evidence for each. Missing dimensions,
unbounded scope, unverifiable metrics, or undocumented resource assumptions
keep the package in REWORK or BLOCKED.

- Solver contract exists at `common_task/environment/input/task_contract.json`. Every input and output has a fixed absolute Solver path, purpose, format, fields/schema, `required`, and data responsibility. Contract runtime has `DATA_DIR=/task/problem/data`, `OUTPUT_DIR=/workspace/output`, working directory, and an entrypoint such as `python /workspace/run.py`. Formal scoring is automatic private verification after submission; public validation commands, when present, must be genuinely callable. The success condition refers to real finite metrics.
- Public package documentation is mounted at `/task/problem/README.md` and `/task/problem/data_description.md`. Verify those exact paths are listed in instruction.md when present in the contract. A host path under `/u01/`, `data/README.md`, or a private evaluator path is invalid as a Solver input.
- Instruction, README, data description, contract, Compose, `test.sh`, verifier, and evaluator agree on paths and filenames. Every declared output is checked by the verifier. Every verifier-read output is declared. Public documents never reference private `/task/evaluation` or hidden labels.
- Formal resources are bound to real paper experiments and are either delivered with nonempty validated bytes or explicitly deferred with identity, version, schema, split, preparation command, and Stage3 runtime validation. Empty/placeholder HDF5/H5AD/ZIP files and invented model/checkpoint paths are failures.
- Check the evaluator ABI exactly. `units[]` uses `id` and `path`, direct single-file submissions use `{submission}`, and `aggregations[]` uses `output`, `operation`, `metric`, and `unit_ids`. `reward_metric` must name an aggregation output. Treat aliases such as `unit_id`, `aggregation_id`, and `method` as repairable schema errors only when the mapping is unambiguous.
- The task covers the paper's complete main experiments when `assessment.scope.scope_policy=full`. A bounded task may select a source-backed subset when `scope_policy=bounded`; verify its task scope, adaptations, difficulty impact, and source evidence instead of requiring an interactive full-panel decision. Each declared experiment has statement, population, split, deliverable, metric unit, aggregation, formal resource IDs, quality requirement IDs, and source evidence. Every paper figure/table has a `covered`, `duplicate`, `schematic`, or `outside_scope` decision with caption evidence.
- Check `assessment.scope.difficulty_profile` and record its computed distance and scope policy in `review_report.json`. Enforce the 20% overall gap and one-level split-drop limits, reject undocumented scope reductions, preserve independent evaluator reference data, and preserve any required model/checkpoint. A self-reported score or rule-only replacement is a failed alignment check.
- The evaluator performs a scientific metric calculation using independent reference/analytical/objective data. JSON shape or constant checks alone fail. Smoke must distinguish correct and weak-valid submissions, reject invalid/empty submissions, and test fixed denominators, missing units, equivalent packaging, and the principal calibration control.
- Solver access is declared. This run permits online scientific search, and the public instruction records the difference from the original offline benchmark. Stage 01 uses fixtures only. Full data/checkpoint acquisition and actual split/file reads are Stage3 execution responsibilities.
- `review_report.json.status` must use the shipped schema values `complete` or `blocked`. Keep it `blocked` while scientific findings or construction checks remain unresolved. `PASS`/`REWORK`/`BLOCKED` are the host contract classification, not report.status values. `complete` requires source review and real metric controls; full-resource verification may remain execution-pending. Use `task_contract_fail`, `agent_capability_fail`, `infrastructure_fail`, or `scientific_validity_fail` only for Stage3 runtime/Judge attribution, not for design status.

Stage2 certifies construction. If all construction checks, source review and
fixture controls pass, authorized deferred resources alone do not belong in
`unresolved_blockers` or `construction_pending`. Set the report `complete`,
retain `real_data_verified=false` and `runtime_verified=false` when appropriate,
and record each outstanding formal resource/check in `execution_pending` with
its Stage3 preparation/validation completion condition. Do not claim its bytes
were verified. Missing authorization, identity/schema, runnable preparation or
independent validation is still a construction blocker. Never override an
unresolved scientific finding to make a report complete.


Preparation review must inspect each referenced script in the delivered package,
not infer executability from an argv string. Stage3 runs from `/prepared`, with
the disposable public inputs at `/task/problem`, private evaluator at
`/task/evaluation`, and `DATA_DIR=/task/problem/data`. Runtime downloads need
actual retrieval/transformation code and independent identity/schema/split
validation; preprovided resources need an explicit source or operator-approved
mount. A check that merely asserts a missing destination exists is not a
download. Full data preparation is execution-pending, not a Stage2 full-scale
run requirement. Keep acquisition locators private. Every smoke fixture must
be declared in `execution.json.data.sources`; `protocol.json.resources` is
derived from it, including on selected-case `--bind-draft` runs.

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

The initialized construction_audit is deliberately incomplete. Fill each public_documents entry with exact quoted claims, subject/field/value and rationale; retain the actual declared resource and requirement IDs. For scope_decisions fill retained_capability, removed_capability, justification, difficulty_effect and source_evidence. For data_paths verify the transcribed content, availability and preparation/validation argv against the final execution.json, then provide consistency_reason, missing_data_behavior and extent evidence. For shortcut_checks bind a real smoke case and its covered requirements, or explain non-applicability with code evidence. Do not invent evidence or mark controls pass without their archived results. After reviewing the final revision, run `python /task/input/pipeline_tools/review_bindings.py --reviewed`, then preflight. Binding refreshes identities and existing evidence; it does not confer acceptance.

Check whether every explicitly mandatory scientific process in the public task is represented by an assessment requirement and an actual evidence/review path. A metric-only evaluator cannot prove a required implementation or training process. Preserve freedom of implementation and equivalent methods whenever the task permits them; optional background methodology must not become a restriction. Do not approve a required method merely because the final values look plausible.


## Exact review report fields

Use the initialized report and actual validator vocabulary. A completed review
must contain your evidence and judgments; these strings are schema values,
not conclusions supplied by the framework:

- `scientific_checks.scope_fidelity`: `status`, a nonempty `evidence` string
  describing source coverage/adaptations/core difficulty, all four
  `claim_verdicts` with `status` and `rationale`, and one `main_experiments`
  decision per actual protocol experiment. `review_bindings.py --reviewed`
  binds the final protocol hash; it does not fill the judgments.
- `construction_audit.public_documents[package_relative_path]`: use verdict
  `consistent` only after checking the document. Transcribe exact public
  `quote`, `subject` (`resource:<actual id>` or `artifact:<actual id>`),
  `field`, and its typed `value`. Check every public formal input's `origin`
  and each declared count/shape/member list against its contract. Keep public
  data origins method-neutral and omit original source addresses.
- `scope_decisions[].verdict` is `justified` only after source review; fill
  retained/removed capability, justification and difficulty effect. The
  package-local evidence citations need `path`, `sha256`, and a real exact
  `quote` or nonempty `locator`. Private original-source excerpts may be
  retained under evaluation for these citations, never in public files.
- `data_paths[].verdict` is `consistent` only after comparing current resource
  declarations and preparation code. `extent_check` is `content` only if the
  declared count/shape/members actually enforces extent, or `custom_validator`
  with citations to the actual independent validation code. Neither means
  deferred formal bytes have already been verified.
- `shortcut_checks[].disposition` is one of `legitimate_method`, `rejected`,
  `bounded_score`, or `not_applicable`. The first three bind a real case ID
  whose `requirement_ids` cover the reviewed requirements. `not_applicable`
  needs a scientific/code rationale and has `case_id=null`; do not use it to
  hide a missing executable control. Cite existing package files.
- A deferred `resource_readiness` check is `phase=execution,status=not_run`
  with nonempty `evidence` explaining what is missing and a precise
  `completion_condition` for Stage3. A complete construction review may retain
  this state, but may not defer failed smoke/calibration controls.

After the final source/code/document repairs, regenerate framework bindings,
rerun affected controls, inspect their archived results, finish your report,
then run `review_bindings.py --reviewed` and the final preflight. If any task
file changes afterward, repeat the affected review/control and binding steps;
never mark a stale report complete. Do not declare complete while any actual
scientific finding or required construction check remains unresolved.
