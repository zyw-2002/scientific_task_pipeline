# Iteration 006

Date: 2026-11-26 (Asia/Shanghai)

This revision starts after the real Iteration 005 Stage2 run. Iteration 005
selected ten tasks from the complete 100-task pool and completed two genuine
Qwen authoring/repair attempts per task; all ten were blocked before Stage3.
The failures were generalized into pipeline changes rather than edits to any
case:

- the Solver-visible `task_contract.json` no longer publishes the private
  evaluator command or evidence root; it uses a portable relative evaluator
  token while the Harbor collector remains in the private execution wiring;
- Stage2 construction and repair prompts now require resource/experiment
  bindings before prose, exact quote lookup instead of guessed paper claims,
  executable calibration mutations, and fixture-bound scoring smoke cases;
- the prompts explicitly prohibit source locators, original result claims and
  private paths in all public artifacts.

The deterministic unit suite passed (29 tests). A new random batch was sampled
from the same 100-task pool with seed `20261127`:

`S005, S076, S034, S039, S070, S019, S022, S037, S051, S031`.

The selection and post-selection readiness evidence are in
`runs/iterations/iteration-006/selection.json` and `preflight.json`. Real
Stage2 is run with up to ten concurrent workers. Stage3 and external QTD/T0
scoring remain gated on actual `construction_ready` evidence; unavailable
scientific or trajectory scores stay null.

The earlier automatic-review egress block was resolved by the user's explicit
Stage2 authorization on 2026-11-26. The real batch completed using the frozen
revision `756bf4e`, the recorded ten task IDs, and the local image
`orginalbench:trajectory-qwen0251-v1`. Each task had up to two construction
attempts and one independent review attempt. Final Stage2 results:

- 1/10 construction-ready: S022, after real independent review.
- 8/10 blocked by construction/review evidence; S005 passed static construction
  checks but its independent review failed with 83 validation findings.
- 1/10 failed Harbor trial: S034; its scientific score is unknown.

S022 entered the real Stage3 workflow with the existing image, public-resource
preparation, Blind Solver, and the configured final judge. Its scientific score
and T0 remain pending until execution and judging complete. The other nine
have no new Stage3 trajectory. See runtime `stage2-summary.json` for check IDs,
review errors, and package paths; no case was replaced.

The preserved external Q/D rubric ran twice per collected public task package.
The nine packages received **Q0=99.583, D0=64.722** (3 review-required). S034
produced no collected package and remains unscored. These are query-only scores
with 9/10 coverage, **not** a passing ten-task batch. They are not directly
comparable to the previous fixed batch's Q0=93.25/D0=72.59, and the high Q0
does not override construction or runtime failures. No promotion is justified.

Candidate fixes discovered while reviewing the actual trajectories are kept on
`iteration-007-public-evaluation-review`. In particular the previously
advertised relative evaluator command is replaced with an honest automatic
private-verification interface; no inaccessible command is described as
callable by the Solver.
