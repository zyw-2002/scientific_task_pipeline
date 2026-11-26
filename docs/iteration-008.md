# Iteration 008 (candidate)

Date: 2026-11-26 (Asia/Shanghai)

This revision follows two real-run findings, without editing either case.
In Iteration 007, S082 was terminated after its first construction attempt
because formal data were missing and a preparation declaration was invalid.
The scheduler allowed another attempt only if its local source inventory
already contained nonempty code/data blobs. That excluded designs whose
legitimate data source or preparation recipe could still be recovered from
the paper. The new scheduler grants one preparation-repair attempt within the
configured construction budget independently of local blob presence. Static
and runtime resource validation remain mandatory and no bytes are invented.

In the real Iteration 006 S022 Solver run, the public task required a neural
wavefunction and VMC training but the observed implementation used another
quantum-chemistry computation. This is a live observation, not a final verdict.
Authoring/review prompts now distinguish free method choice from explicit
process requirements and require the latter to have an evidence/review path.
Solver guidance clarifies that general permission to use a simple method
does not override a task's explicitly required scientific process. Independent
implementations remain allowed; no specific package is imposed.

All 40 tests pass. The next uniform random sample (seed 20261129) is:

`S003, S055, S087, S038, S021, S006, S051, S040, S093, S099`.

The sample is frozen before readiness filtering. Iteration 007's in-flight
workers retain their own frozen code and prompts; this candidate will execute
as a separate batch after those construction workers finish, with concurrency
up to ten. It is not promoted to main without complete comparable Q/D/T and
real Stage3 evidence.

The final T-P/F1--F8 scoring client is now implemented as
`pipeline.orchestration.semantic_scoring`. It sends only explicitly public
document roles and trajectory steps, excludes previous aggregate scores and
private evaluation material, and rejects silent input truncation or model tool
calls. The model output is checked against the original trajectory SHA-256
and exact per-step quotes before points contribute. Requests/responses and
prompt/input identities are archived outside tracked source. A real final
process review of the corrected NatureBench sample has been started; malformed
or unbound evidence remains unknown. All 41 tests pass.
