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

At execution time, the local automatic review rejected the ten-worker launch
because this configuration sends private source bundles and authenticated
authoring interactions to the public Qwen endpoint. The command was not
rerouted or bypassed, and no Iteration 006 Stage2/Stage3 score is claimed.
The committed revision is ready to run once the endpoint boundary is explicitly
resolved.
