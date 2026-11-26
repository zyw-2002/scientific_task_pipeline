# Cross-benchmark trajectory evidence review

This note records what the existing trajectory work contributes to the
pipeline. It is an engineering input, not a new benchmark score and does not
send any task package or trajectory to an external Solver.

## Existing analysis_tractory

The previous analysis pipeline at
`/u01/zhengyawen/pipline/analysis_tractory` has two useful layers:

* deterministic accounting (`metrics-only`) keeps model decisions, tool calls,
  timing, usage, cost, first valid submission, and linked hashes separate;
* an optional semantic review uses F1--F8 plus `ENV`: goal/constraint,
  strategy, interface, data/resource, implementation, observation/evidence,
  output/reporting, stopping/recovery, and environment failures.

Its safeguards are directly applicable: do not infer an error from absent
behavior, do not blame an environment failure on the Solver, do not count
normal retries as separate errors, and require exact frozen-trajectory quotes
for a proposed issue. The review output is explicitly model-proposed rather
than human gold, so it must remain a separate semantic-review record. The new
T0 score therefore uses deterministic evidence for its number and can attach
the normalized F1--F8/ENV review as an explanatory layer.

The historical summary contains 458 frozen runs, 62 reviewed runs and 95
proposed issues. The issue distribution is F5=37, F3=15, F4=15, ENV=12,
F1=7, F6=4, F2=4, F7=3, F8=3. This suggests that implementation, interface,
and data identity checks deserve more recovery guidance than simply adding
more scientific background to a query.

## NatureBench

The current NatureBench job inventory at
`/u01/zhengyawen/s1-harbor/jobs/NatureBench` contains a best-manifest with 91
selected trials and recorded `trajectory_present` flags. The raw job tree also
contains ATIF `trajectory.json` files. These trajectories are valuable for
calibrating tool-use, data inspection, validation, and stopping signals, but
their reward is not a trajectory-quality label. T0 must preserve that
separation: reward/scientific score is an outcome, while trajectory evidence
is whether the path supports the outcome.

## ScientistWorld

`/u01/zhengyawen/bench/ScientistWorld-prepared` currently has 62 prepared
task packages with visible instructions and no main `trajectory.json` or
`events.jsonl` files. Its packages are useful as query/specification examples
and for red-team cases (copied labels, forged scores, empty outputs,
placeholder data, and cherry-picking). They cannot yet calibrate a trajectory
distribution until runs are produced. Their visible documents also demonstrate
why source-locator scanning must be a public-boundary gate: a package may have
rich private/reference evidence while the Solver-facing problem stays
method-oriented.

## Pipeline consequence

For each new random ten-task batch, the records should retain three explicit
namespaces:

* Q0: construction/query quality, including the six existing quality gates;
* T0: deterministic trajectory evidence, with an observed score and explicit
  coverage/unknown dimensions. Optional F1--F8/ENV semantic review is attached
  as diagnosis with exact evidence references; it is not silently folded into
  the number;
* scientific: the Stage3 evaluator metric and validity/Judge result.

A change to prompts or orchestration is accepted based on the aggregate Q0 and
scientific results over the newly sampled tasks, while T0 diagnoses why a
scientific score did or did not improve. T0 is only comparable when its
coverage is reported; unknown dimensions are not zero-filled. No namespace is
substituted for another and missing trajectories remain unscored.
