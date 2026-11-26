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

I attempted a real current Stage2 reconstruction for S080 using the local
prebuilt image. The image exists locally under the recorded digest, but the
automatic safety review rejected the command because the configured public
Qwen endpoint would receive the private source bundle and authenticated
authoring interaction. The run therefore stopped before the worker started;
there is no false Stage2 or Stage3 success claim. The existing historical
packages also fail the current Stage2 gate on solver contracts, evaluator
bindings, resources, and public/private boundary checks. No new Stage3 run is
counted in this iteration.

The next execution step requires an explicitly approved local/private worker
endpoint or an equivalent command backend that does not export source bundles.
Once that execution boundary is available, the same random batch will be run
with up to ten concurrent workers, followed by Stage3 and T0/scientific
scoring. The external QTD judge remains limited to the final scoring phase.
