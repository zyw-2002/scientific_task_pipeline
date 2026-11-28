# Iteration 010 (candidate)

Date: 2026-11-28 (Asia/Shanghai)

Iteration 008 has now completed all ten real Stage2 runs, including S099
after service recovery. All ten are blocked before Stage3. Its terminal
packages remain unchanged, and a preserved two-repeat external Q/D review is
running. Query scores cannot establish runnable task quality.

Iteration 009 launched at revision `d93dfe2` with ten concurrent workers on
its unchanged selection. The exported first-attempt trajectories show actual
source-index use and 6–14 exact-quote lookups per task. They also expose
remaining ABI failures. These are observations, not evidence of improvement;
the workers continue using their original immutable snapshots.

This candidate repairs concrete general defects:

- Source evidence index regex/newline escaping produced invalid JSON and
  missing section anchors. The index now round-trips as JSON, uses actual LF
  source lines, verifies primary file containment and bytes against hashes,
  and must reside at the private package root. The previous file-presence
  test missed the serialization defect; it now reads the index back and
  rejects changed source bytes.
- The collection-time smoke evidence validator used a separate naive path
  join even though the runner accepts both configuration-relative and
  package-relative fixture paths. It now uses the same checked resolver.
  A real local evaluator subprocess runs correct, weak-valid and invalid
  controls, emits capture/score events and is accepted by host evidence
  validation; changing a fixture invalidates the saved receipt.
- Missing scientific handoff wiring now defaults to the existing full smoke
  runner, which actually captures submissions, evaluates, records and checks
  evidence. The previous fallback copied a scoring command that might have
  invoked a measure-only adapter without a CLI. Existing explicit handoff
  declarations remain inspectable; no metric or acceptance is invented.
- The citation scanner previously matched ordinary `cell-type` schema prose
  followed by a cell count such as 2017. Journal-plus-year citations still
  fail, while these scientific data descriptions no longer fail solely due
  to that coincidence. Both compatibility and public validators are tested.
- A blank calibration scaffold exposes the actual suite/replacement ABI.
  Prompt guidance distinguishes a disposable evaluator mutation suite from
  output perturbation commands, and distinguishes InvalidSubmission from
  genuine dependency/runtime errors. The suite and faulty implementation
  must still be written and executed by the constructor. No false pass is
  synthesized. Source-scale/split/metric adaptations may use existing
  task_policy/not_specified origins with explicit rationale; paper-grounded
  claims still require exact source evidence.

Validation: all 63 tests pass, including the real evaluator receipt round-trip.
The public authoring prompt and the actual frozen-worker prompt are in sync.

Selection: uniform sample from all 100 unchanged seeds, seed `20261201`,
chosen before readiness checks:

`S019, S032, S020, S030, S025, S022, S009, S011, S018, S042`.

Preflight: 5 ready, 1 downloadable, 4 partial. All ten frozen primary source
bundles are bound. This candidate is queued behind Iteration 009; it will not
change an in-flight worker or replace a selected case. Main is not promoted
without complete comparable Q/D/T and actual Stage3 evidence.

Execution checkpoint on 2026-11-28, 14:20 Asia/Shanghai: the Iteration 008
external Q/D requests all returned HTTP 429 and yielded zero valid judgments;
both batch means remain null with 0/10 coverage. A single minimal request
confirmed the exact `insufficient_quota` error again, saved as Iteration 009's
`endpoint-diagnostic.json`. A previously successful probe did not establish
sufficient quota for sustained workload. The waiting Iteration 010 supervisor
was stopped before launching any worker. Existing Iteration 009 child
processes and snapshots remain available for their own terminal export.
Nine were still running and S045 was blocked at this checkpoint.

The user explicitly authorized Stage3 Qwen Solver input/interaction egress
on 2026-11-28 after automatic approval review requested a separate Stage3
authorization. No Stage3 was actually launched by the waiting supervisor.
This permission applies when the original endpoint becomes usable again.

Recovery on 2026-11-28: a minimal API call returned 200, then the original
rubric completed both real S003 judgments (Q0=98.75/D0=71.875). A serial
recovery scorer now imports those exact responses only after request/model
hash validation, retains the frozen ten-task cohort and stops future requests
on HTTP/quota errors. No quota-blocked response is treated as a low grade.
The resumed cohort has completed two judgments each for S003, S055, S087 and
S038 so far; its full-batch scores remain pending.

The restored workflow waits for the existing Iteration 009 workers, collects
their original final packages and runs the same serial rubric. Only native
construction-ready packages proceed to real Stage3. The subsequent frozen
Iteration 010 uses four constructors and one Solver at a time to reduce
request bursts, within the user's ceiling of ten. Missing task packages stay
unknown in the original denominator. Scientific outcomes and full T0 remain
separate from query-only Q/D. Runtime script identities and selection hashes
are recorded in `recovery-plan.json`; the state is in `recovery-state.json`.

The first real Iteration 009 S043 independent-review export passed static
construction but failed host acceptance on missing/stale audit references,
incomplete public declaration transcriptions and incorrect audit vocabulary.
Review guidance now spells out exact existing status values, citation fields,
deferred-execution semantics and final evidence-binding order. This does not
automatically change a report verdict, synthesize a reference or loosen a
scientific acceptance check. In-flight Iteration 009 snapshots are unchanged.

## Recovery outcome and trajectory-driven follow-up

At 14:47:52 Asia/Shanghai on 2026-11-28, the serial external rubric received
HTTP 429 `insufficient_quota` on S099's first judgment. Nine selected tasks
completed both judgments: Q0 mean 99.305556, D0 mean 63.194444. Coverage is
9/10, S099 remains null, and the cohort did not pass. All ten Iteration 008
construction outcomes remain blocked and T0 remains unknown. The successful
calls demonstrate a period of service availability, not durable quota recovery.
The supervisor stopped before any Iteration 010 worker was launched.

The real S075 first construction trajectory, steps 93--121, repeatedly edits
only `protocol.json.resources`, then loses those edits during regeneration or
selected-case `--bind-draft`. The generator correctly rebuilds that derived
table from `execution.json.data.sources`; retaining undeclared resources would
break the canonical resource contract. The authoring prompt, however, called
public examples "fixtures" despite the validator requiring private evaluator
fixtures. Clarify canonical declarations, public development inputs versus
private fixtures, and why both generation and selected-case smoke rebind them.
Missing binding errors now name both IDs and the editable declaration location.
No new gate, resource substitution or automatic scientific verdict is added.
Also remove an inconsistent unconditional block for missing resource bytes:
a genuine deferred preparation remains execution-pending, while an absent
authorized preparation path still blocks. Correct the generated public
task-contract path in the final-check instructions.

A regression executes the real evaluator in a subprocess: a protocol-only
fixture fails with an actionable canonical-declaration error; declaring it in
the execution contract survives repeated rebinding and produces the expected
metric without formal data bytes. All 64 tests pass.

At 14:54, Iteration 009 S043 and S075 have passed host construction acceptance.
Their original packages remain frozen. Real isolated Stage3 resource
preparation is the next executable check; Solver execution and full T0 are
still pending. This is not a claim that query-only scores prove run readiness.
