# Iteration 009 (candidate)

Date: 2026-11-26 (Asia/Shanghai)

This candidate follows real Iteration 007 author/reviewer trajectories while
Iteration 008 continues under its frozen Stage2 program. Iteration 007 has no
construction-ready task. Query-only Q0=99.444/D0=67.014 covers nine packages;
it does not establish executable task quality or a passing ten-task result.

S094 passed all static construction checks, yet its independent reviewer
left the package blocked only because formal resources require preparation
in Stage3. The launcher also used the legacy execution-readiness scope by
default. The standalone Stage2 CLI now defaults to construction certification.
An explicit runtime probe automatically selects execution scope, and
`--no-construction-only` retains the legacy mode. The resolved choice is
preserved through snapshot re-execution. Review guidance explicitly separates
construction blockers from `execution_pending`; verified scientific sources,
source fidelity, valid preparation declarations and real fixture controls
remain mandatory. No report status or failed case is automatically rewritten.

The quote lookup helper and main source-claim validator used `splitlines()`,
while the caption index and caption-evidence validator use physical LF lines.
PDF form feeds and Unicode separators could therefore make genuine helper
output fail a caption check. All three now share physical source line numbers.
A regression exercises lookup, caption indexing, exact hash/line binding and
claim validation together, and still rejects a deliberately false line range.

The initial claim scaffold now exposes the actual blank `statement`, `origin`,
`source_evidence` and `rationale` fields instead of empty dictionaries. This
addresses repeated ABI mistakes observed in six collected packages without
inventing any source claim or scientific decision.

Validation: 50 tests pass on this isolated candidate before incorporating the
separately developed long-trajectory scoring client. No model endpoint is
used for source-code changes or deterministic tests.

The next ten tasks were uniformly sampled from the unchanged 100-task pool
before readiness checks, with seed `20261130`:

`S098, S018, S075, S008, S043, S045, S039, S012, S093, S085`.

Preflight: 1 ready, 1 downloadable, 8 partial. All IDs remain in the denominator.
Real construction/execution has not yet begun for this candidate; a new
score or improvement is not claimed. It remains off main pending comparable
complete construction, Stage3 and rubric evidence.
