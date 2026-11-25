# Stage3 interface (2026-11-17)

`workflow.py` runs one prompt03 Solver session with repeated public self-checks,
accepts one explicit frozen submission, then runs formal rules/scoring and prompt04 content review.
The root experiment runner should prepare a pair once per verified Stage2 build
and call this entry once per model × mode × scenario × rollout.

```bash
python code/workflow.py \
  --pair /absolute/path/pair_manifest.json \
  --variant blind \
  --model qwen3.7-plus \
  --agent qwen-coder \
  --harbor /absolute/path/harbor \
  --solver-base-url http://model-service/v1 \
  --solver-api-key-env SOLVER_API_KEY \
  --env-file /absolute/path/private.env \
  --solver-prompt prompt/03_solver_instruction.md \
  --judge-prompt prompt/04_run_verify.md \
  --judge-endpoint http://judge-service/v1/chat/completions \
  --judge-model judge-model \
  --judge-api-key-env JUDGE_API_KEY \
  --judge-timeout-sec 300 \
  --judge-max-input-bytes 8000000 \
  --judge-temperature 0 \
  --scenario examples/scenarios/attachment.json \
  --rollout-seed 1 \
  --attempt-policy latest-scored \
  --output /absolute/path/new-run
```

The example endpoints are placeholders. `--dry-run` prepares immutable inputs
and the actual Harbor argv without launching Docker or models. `--package`
can replace `--pair`, preparing the pair inside this run. Existing output is
never overwritten. Construction acceptance and revision bindings are required; runtime probes and data readiness are not launch gates. Bind a prebuilt image with `--package ... --task-image IMAGE`, or pass `--task-image` when preparing the pair. The root runner controls resume with completed receipts.

`--agent-kwarg NAME=JSON` repeats and forwards real Harbor `--ak` arguments.
For example Terminus supports `--agent-kwarg 'temperature=0.7'` and
`--agent-kwarg 'max_turns=40'`. Installed Qwen does not expose those parameters;
unsupported seed/temperature/token/turn knobs are rejected. `rollout_seed` is
an experiment identifier; it does not claim deterministic provider sampling.
For a supporting adapter, provider seeds must be explicitly supplied via its
actual supported parameters and assessed with provider documentation.

An env file uses literal `NAME=value` lines, never shell execution. Credentials
are read into child environment, not argv or run JSON. Existing pinned download
and build proxy variables are protected from env-file overrides.

Modern tasks produce exactly one formal SCORE_ATTEMPT, bound to the final submission receipt. Self-checks are never scoring events. Missing formal submit yields a missing-submission event, never an implicit submission of live output.

For historical exports, `latest-scored` chooses the last chronological SCORE_ATTEMPT, including failed
or missing-submission attempts. It never selects by hidden metric values.
`--attempt-policy explicit --attempt-id ID` selects one declared attempt.

## Results

- `input/effective/scenario_manifest.json`: private definition, input hashes,
  actual public/solver file references. Never mount the manifest into Solver.
- `03_solver/`: exact run manifest, request, workspace, Harbor artifacts,
  execution log, runner lifecycle events, bound `run_export.json`.
- `04_run_verify/evidence/`: immutable scoring snapshot and actual inputs.
- `04_run_verify/review/`: verdict with six required fields and optional
  evidence-backed `failure_attribution`, seven-class reporting manifest,
  successful/rejected original response, provider usage and latency when known.
- `resolved_config.json`, `command.sh`, `result.json`: run configuration,
  solver command preview, and machine-readable cross-stage links.

Workflow completion is separate from validity and score. Result states include
`complete`, `dry_run`, `solver_failed`, `missing_score_evidence`,
`judge_not_configured`, `judge_rejected`, `insufficient_judge_evidence`,
`scenario_incomplete`, `rules_failed`, `rules_error`, `error`. Rule failure/error skips the content Judge and does not restart Solver. Original solver execution failure is retained
even if it produced usable scoring evidence. Never convert missing scores to 0. A nonzero Solver exit remains `solver_failed` even when a prior attempt can be scored and reviewed. A Judge-declared `insufficient_evidence` result is `insufficient_judge_evidence`, just like framework-detected missing evidence; neither is reported as `complete`.

## Scenarios

`examples/scenarios/canonical.json` preserves the task. `attachment.json`
losslessly moves the verified README bytes to `attachments/task_brief.md` and
replaces README with a short pointer. The version-bound English execution text for the Chinese prompt03 is delivered. Both source texts are archived outside Solver mounts; changing either without updating the reviewed binding fails preparation.
No generated rewrite or missing scientific fact is invented.
The generated Solver instruction renders only `{{TIME_LIMIT_MINUTES}}` from
the actual runtime and rejects unresolved placeholders. CPU, memory, GPU,
seconds, network and search permissions are appended explicitly. The full
standard scientific contract is never appended to an omission scenario.
`search_policy=none` forbids external research retrieval; authorized task data,
weights and dependency downloads remain subject to their separate network and
task permissions. The raw prompt03 source and English execution text are preserved in `input/effective/prompts/` with hashes in `scenario_manifest.json.solver_guidance`. Custom prompts without the bilingual marker are delivered literally. The hash binding verifies versions, not translation semantics.

Custom versions use the following schema; replace the example file hash with
the actual SHA-256. Relative source paths resolve beside the scenario JSON.
Review fields are author assertions, not machine proof of equivalence.

```json
{
  "schema_version": "pipeline-scenario-v1",
  "scenario_id": "oral-v1",
  "kind": "natural",
  "task_id": "the-specific-task",
  "readme": {"path": "oral.md", "sha256": "ACTUAL_SHA256"},
  "review": {
    "approved": true,
    "reviewer": "reviewer identity",
    "basis": "Which scientific facts and access boundaries were checked",
    "information_equivalent": true,
    "public_exposure_checked": true
  },
  "attachments": [
    {"source": {"path": "notes.txt", "sha256": "ACTUAL_SHA256"},
     "destination": "attachments/notes.txt", "role": "noise"}
  ]
}
```

Kinds: canonical, natural, attachment, noise, ambiguity, multiturn.
Natural/attachment/noise must declare information equivalence. Ambiguity can
explicitly remove information only after task-specific review. Review must
consider alternate public disclosure paths: code, data metadata, annotations,
Guided Method, and other attachments. The framework does not claim to prove
scientific equivalence or absence of semantic leaks. Private source package
files cannot be attached; supplied public files may not overwrite existing data.
`role:noise` stays private; attachments use the author's chosen public filenames.

## Scripted interaction

The supported mechanism is **cooperative file mailbox**, not native chat
injection and not automatic detection of semantic progress. Add this block to
a reviewed, task-specific scenario:

```json
{
  "interaction": {
    "mode": "mailbox-v1",
    "events": [
      {"id": "update-1", "trigger": {"type": "milestone", "name": "initial_analysis"},
       "text": "An actual task-specific fact supplied by the scenario author"}
    ],
    "clarifications": [
      {"key": "sample_unit", "answer": "The actual experimental-unit definition"}
    ],
    "unmatched_reply": "No additional information is available; state necessary assumptions."
  }
}
```

Only `start` and explicit `milestone(name)` triggers are supported. Native chat,
wall-clock/turn-count injection, free-form semantic question matching and
changing enforced resource limits are not implemented and unknown schema or
triggers fail before a model runs. Do not treat the text of a budget change as
an enforced resource-limit change.

Solver receives protocol instructions to atomically write requests under
`/task/interaction/outbox/<request_id>.json` and read its response from the
readonly `/task/interaction/inbox/`. A host controller answers exact-key
clarifications and publishes scripted messages at milestones. Pending script
facts and answer keys are not mounted. IDs and file sizes are validated.

Messages are timestamped after writing to the inbox. Delivery is not evidence
that the model read or used a message. Unreached events yield
`scenario_incomplete`, retaining scientific score and verdict separately.
Prompt04 sees only interaction records at or before formal submission (the selected score for legacy exports), not
undelivered future events or hidden answer tables. Stage4 can analyze all
actually delivered interactions through `artifacts.interaction_events`.


## Paper comparison

`result.json` preserves `scientific_score` and the existing Judge verdict, and adds
`paper_comparison`. Stage2 declares and reviews `assessment.paper_comparison`;
new constructions require `paper_comparison_version=1` in host-owned metadata.
The selected attempt's protocol SHA, formal resource evidence, frozen submission,
and private reviewed reference are bound before gap calculation. The frozen bundle
contains `paper_reference.json`; offline interpretation does not read a live task
protocol. The Judge remains a validity reviewer and does not compute the gap.

A computed gap is `direction * (actual - paper) / abs(paper)`, with direction +1
for higher-is-better and -1 for lower-is-better. Per-unit anchors require complete
unit coverage and average individual gaps. Zero anchors, missing metrics,
fixture/authoring runs, diagnostic runs, stale/unverified references and mismatched
protocols produce an explicit status and `g: null`. They do not overwrite raw
scores or invent scientific zeros/failure gaps.

`eligible_for_paper_comparison` is true only when the workflow completes, the
accepted Judge verdict is valid for the same attempt, submission hashes agree,
and the comparison was computed from formal evidence. An invalid or unreviewed
trajectory can retain its computed diagnostic gap but cannot enter eligible gap
statistics. Batch CSV/JSON exports include comparison status, gap and eligibility;
summary means remain separate by task and frozen reference revision. This is
not a blanket claim of scientific superiority over the source paper.


## Executable submission protocol

The effective task mounts a public `submission_client.py` for Solver, and a private
controller in the existing evaluator service. The controller preserves the original
service command as its bootstrap child. Separate named volumes carry requests and
read-only replies; no network endpoint is needed. Solver never mounts evaluator
snapshots, protocol tools, private references or the rule report.

`check` captures current candidate bytes and validates declared public structure,
without importing the task metric adapter or exposing private ID sets/scores.
`submit` uses a private lock and atomic receipt to commit exactly once. Retry the
same request ID after a timeout. After Solver exit, the verifier collect command
closes submission and evaluates only acknowledged bytes. Self-check results live
in `selfchecks.jsonl`; they are not formal attempt selection candidates.

`run_export.verification_files` binds `rule_checks`, `final_submission`, and optional
`selfchecks` to the exact run boundary. Both workflow and the direct Judge entry
validate their frozen hashes and attempt IDs. Hard-rule failure and checker errors
produce deterministic `rules_failed`/`rules_error` manifests with `model_called=false`
and no substantive content assessment. Applicable checks must pass before either
an API Judge or an imported verdict is accepted. Paper comparability remains a
separate statistic and never blocks content review.

Trusted command adapters for modern tasks must implement this lifecycle and export
the same evaluator-owned receipts; an old export lacking them is rejected. Adapters
own their isolation. Legacy tasks without a declared evaluation protocol retain
explicit `legacy_scoring_export` mode for historical analysis.
