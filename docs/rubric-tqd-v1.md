# T/Q/D rubric v1

This is the fixed evaluation contract for future pipeline iterations. `Q`,
`D`, and `T` answer different questions and must be reported separately.

## Q: task/query construction quality (Q0, 100 points)

Q evaluates whether the task is well constructed for a fair, reproducible
scientific run. A high Q score does not mean that the task is easy or that a
Solver will succeed.

| ID | Dimension | Weight | Required evidence |
|---|---|---:|---|
| Q1 | Objective and constraint clarity | 20 | scientific objective, inputs, constraints, and deliverables are unambiguous |
| Q2 | Scope and workload boundedness | 20 | finite task scope, realistic time/compute budget, no hidden open-ended work |
| Q3 | Solvability and resource completeness | 20 | complete schema, authorized resource path/preparation, no impossible or missing dependency |
| Q4 | Evaluability and acceptance contract | 15 | executable evaluator, fixed output contract, independent metric and edge cases |
| Q5 | Scientific relevance and source fidelity | 15 | source-backed objective, population, split, metric, and justified adaptation |
| Q6 | Reproducibility, fairness, and leakage control | 10 | versioning, deterministic bindings, fair access, and no answer/source locator leakage |

Each dimension is judged on 1–5 or `null` when the task-facing evidence is
insufficient. Missing information is not converted to zero. Q0 is complete
only when all applicable dimensions have evidence. The existing external
rubric names are `q_clarity`, `q_scope`, `q_solvability`, `q_evaluability`,
`q_relevance`, and `q_reproducibility`.

## D: objective task difficulty (D0, 100 points)

D evaluates the work required by the task under its declared resources. A high
D score is not a construction defect and should never be used to lower Q.

| ID | Dimension | Weight | Required evidence |
|---|---|---:|---|
| D1 | Reasoning depth | 25 | number and depth of scientific inferences, modeling choices, and checks |
| D2 | Dependencies and decomposition | 20 | dependency graph, cross-step coupling, and need for coordinated subproblems |
| D3 | Tools and environment complexity | 20 | software, data formats, hardware, external services, and setup burden |
| D4 | Long-horizon execution and state | 20 | expected steps, persistent artifacts, iterations, and state tracking |
| D5 | Uncertainty, failure cost, and recovery | 15 | ambiguity, noisy data, error impact, and recovery/search burden |

The external rubric names are `d_reasoning`, `d_dependencies`, `d_tools`,
`d_long_horizon`, and `d_uncertainty`. D describes the task before observing
a particular Solver trajectory.

## T: Solver trajectory quality (T0, 100 points)

T evaluates what the Solver actually did and what the trajectory proves. It
does not replace the scientific result. T0 has a 60-point mechanical evidence
layer and a 40-point semantic process layer.

### T-E: mechanical execution evidence (60 points)

| ID | Dimension | Weight | Evidence |
|---|---|---:|---|
| T1 | Formal input use | 12 | frozen input manifest, matching run manifest, and input-read evidence |
| T2 | Data integrity | 12 | identity/bytes match, no synthetic substitution or placeholder input |
| T3 | Contract adherence | 9 | output contract, submission snapshot, and scoring-attempt evidence |
| T4 | Real execution | 12 | real Solver run, tool calls, timeline, trajectory, and export |
| T5 | Validation and recovery | 9 | independent validation, error handling, retries, and frozen evidence |
| T6 | Claim support | 6 | completion claims agree with submission and Judge evidence |

### T-P: semantic process quality (40 points)

The former F1–F8 labels become positive process criteria only when a semantic
review binds each judgement to exact trajectory evidence:

| ID | Criterion | Historical label | Weight |
|---|---|---|---:|
| T7 | Goal and constraint adherence | F1 | 5 |
| T8 | Strategy choice | F2 | 5 |
| T9 | Tool and interface use | F3 | 5 |
| T10 | Data and resource identity | F4 | 5 |
| T11 | Implementation and computation | F5 | 5 |
| T12 | Observation, evidence, and verification | F6 | 5 |
| T13 | Context and state maintenance | F7 | 5 |
| T14 | Process control and termination | F8 | 5 |

For each T-P criterion, `5` means the criterion is supported by evidence,
`3` means mixed or partially recovered behavior, `1` means a clear severe
failure, and intermediate values require evidence. `null` means unknown; it is
not a zero. Error labels may be multi-valued, but repeated consequences of one
error are counted once.

`ENV` is an environment status, not a Solver penalty dimension. It records
network, Docker, dependency, permission, hardware, or external-service
blocking. A separate F1–F8 issue may be recorded when the Solver responds to
that block incorrectly.

## Reporting and iteration rules

Every iteration records Q0, D0, T0, and the scientific Stage3 score in separate
namespaces. Each score includes per-dimension evidence, coverage, and unknown
dimensions. No missing dimension is zero-filled. Q/D judge inputs contain only
task-facing specifications; T semantic review inputs contain only the public
task, trajectory evidence, and submission evidence permitted by the review
contract. Hidden references, private evaluator data, and authoring source
locators stay private.

Pipeline changes are selected for generality across a fresh random ten-task
sample. A lower score on one case is a diagnostic signal; it is not grounds
for editing that case specifically.

Implementation evidence requirements: an external ATIF adapter's activity
keywords are diagnostics only. T4 needs tool observations linked to call IDs;
the remaining T-E dimensions need their independent native contract/evidence.
For T-P, each `semantic_review.criteria` entry contains `score`, `reason` and
`evidence` records (`step_id`, exact `quote`), and the review binds the current
trajectory with `trajectory_sha256`. Numeric scores without this binding are
unknown. This tightens evidence validation while preserving all dimensions
and weights above.

The text-only final semantic scorer can be run with
`python3 -m pipeline.orchestration.semantic_scoring --task-specs <public-specs.jsonl>
--task-id <id> --trajectory <ATIF.json> --env-file <local.env> --output <new-directory>`.
Its 40-point process score is separate from the 60-point native mechanical
evidence. External traces lacking native input/contract evidence retain an
incomplete total T0 even when all eight process criteria are reviewed.

Long trajectories use the same criteria and weights. The client reviews
consecutive complete behavior steps in bounded segments, extracts observations
with exact source spans, then performs a single global process judgment.
It audits total step coverage and the immutable original hash. Segments are
not assigned point scores or averaged. Final scores are checked against the
original trajectory, and unsupported criteria remain unknown. Reports record
the review protocol so this preprocessing is visible when comparing runs.

Citation validation failures permit one citation-only repair per segment;
observation statements cannot be changed or removed. Identical requests in
unfinished reviews may reuse archived responses, with model/prompt/input
identity checks. A finished review is never overwritten, and neither repair
nor reuse is triggered by the numerical score.
