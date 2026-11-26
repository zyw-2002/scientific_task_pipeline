# Iteration 005

Date: 2026-11-26 (Asia/Shanghai)

This iteration starts from the previous Q0=93.25 and D0=72.59 reference, then
draws a fresh random batch from the 100-task pool with seed `20261126`:

`S061, S057, S005, S080, S086, S011, S038, S042, S082, S029`.

The first resource preflight used stale historical paths and reported
`ready=0, downloadable=1, partial=4, blocked=5`. Inspection showed that the
five blocked rows had accessible `task_definition_candidates`. The pipeline
was changed to discover those candidates while keeping them partial until
data, evaluator, and runtime evidence are complete. The same batch now
reports `ready=3, downloadable=1, partial=6, blocked=0`. A missing resource is
therefore preserved as a readiness state instead of silently being replaced by
another task or prematurely discarded.

QTD results using the authorized external rubric on task-facing specifications:

| Population | Q0 | D0 | Coverage note |
|---|---:|---:|---|
| Fresh OriginBench batch | 62.000 | 75.000 | 10/10 complete; evaluability and reproducibility are the main weaknesses |
| NatureBench observed sample | 89.125 | 73.375 | 5/10 missing judge results; incomplete aggregate |
| ScientistWorld visible sample | 96.625 | 67.938 | 10/10 complete; 4/10 marked review-required |

The T0 structural audit reports `observed_score=39` and `coverage=0.39` for
the fresh OriginBench trajectories and the observed NatureBench trajectories;
the semantic F1--F8 layer and task-contract evidence remain unknown. The
ScientistWorld sample has no Solver trajectories and is explicitly unscored.

The authorized real Stage2 run used the local prebuilt image and the configured
Qwen endpoint. All ten tasks started genuine authoring workers. A second repair
round (`--max-construct-attempts 2`) also completed for all ten, but every task
remained `construction_ready=false`; no Stage3 run was started because the
pipeline gate correctly prevents execution without a verified package. The
dominant general failures were public `task_contract` leakage of
`/task/evaluation`, missing executable calibration/smoke bindings, inconsistent
paper quote evidence, and incomplete resource/experiment bindings. These are
recorded as pipeline diagnostics, not case edits or fabricated scores.

The next revision changes the public contract generator to use a portable
relative evaluator token and keeps the actual collector private. It also gives
both authoring and repair workers an explicit order for binding resources,
paper evidence, calibration and smoke controls while retaining the no-leakage
gate. The following iteration uses a fresh random ten-task sample.
