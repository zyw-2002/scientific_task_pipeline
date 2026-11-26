# Iteration 007 (candidate)

Date: 2026-11-26 (Asia/Shanghai)

This candidate follows the real Iteration 006 construction/review trajectories.
The previous batch yielded one construction-ready package, eight blocked
packages and one failed worker. Its query-only external Q0=99.583/D0=64.722
covers nine packages and cannot establish a valid ten-task improvement. S022
is running Stage3; its result remains separate from these construction scores.

Changes are general framework and review-interface fixes:

- Public solver contract v2 accurately declares automatic private verification.
  It omits private collector commands and evidence paths, and retains optional
  real public validation commands. Existing v1 contracts remain readable.
- Smoke submission mappings such as `{"path":"fixtures/file.csv"}` normalize
  to path strings before validation. Expected metrics and resource identities
  are not invented or loosened by this change.
- The independent-review template transcribes declared resource/requirement
  IDs and current data/preparation declarations. Evidence and rationale remain
  empty, verdicts remain `not_reviewed`, and report status remains `blocked`.
  Existing findings are never overwritten. This addresses the review ABI
  mismatch observed in S005 without manufacturing scientific acceptance.
- Review prompts now use the actual schema status values `complete`/`blocked`
  and instruct reviewers to bind evidence after inspecting the final revision.

Validation: 32 tests pass, including regressions for the public/private scorer
boundary, readable Solver instructions without an invented evaluator command,
smoke mapping compatibility, and the deliberately incomplete review template.

The new ten-task sample is drawn uniformly from the full unchanged 100-task
pool with seed `20261128`:

`S059, S015, S094, S100, S013, S002, S082, S051, S050, S047`.

Selection occurs before readiness filtering (`ready=0, downloadable=3,
partial=7, blocked=0`). All IDs remain in the denominator. Run artifacts will
record source bindings, frozen program snapshots, authoring trajectories,
preflight, independent review, accepted Stage3 executions and rubric coverage.
The candidate stays on its branch until comparable complete evidence supports
promotion; no high query-only score is treated as runtime success.

Additional reporting fixes on the candidate branch prevent a short list of high
scores from passing a ten-task iteration: the summaries require ten unique IDs,
optionally bind them to frozen selection.json, reject nonfinite/out-of-range
scores and partial T0 coverage, and expose D0/trajectory consistently in both
CLIs. Q0=99.583 over nine results now reports 90% coverage and passed=false.
A read-only NatureBench observation adapter also resolves actual public task
packages, excludes administrative jobs and keeps missing documents explicit.
These changes do not alter the ongoing frozen Stage2/Stage3 worker inputs.

Trajectory reporting was subsequently corrected after inspection of actual
NatureBench ATIF steps. Narrative keywords and planned calls no longer earn
execution/validation/input/claim points. Real execution requires observed
results linked to tool call IDs. F1--F8 process points require the current
trajectory SHA-256, per-criterion rationale and exact quotes from named steps;
unbound numeric scores are unknown. Weights remain T-E=60, T-P=40. Validation
now has 38 passing tests. This reporting fix invalidates earlier keyword-based
partial T0 observations; it is not a change to the fixed rubric.
