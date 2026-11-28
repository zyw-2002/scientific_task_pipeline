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
