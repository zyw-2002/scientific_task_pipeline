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

The first real NatureBench T-P requests exposed two scoring-interface issues:
HTTP 429 responses during concurrent runs, and model quotations that normalized
whitespace rather than matching the raw trajectory. Such responses yielded
unknown points, never unverified scores. The client now provides exact
trajectory-span IDs, resolves them to original bytes locally and still checks
the original step/hash binding. It also makes at most three rate-limit
attempts with bounded backoff and transport audit records. This changes the
citation interface only, not criterion values or weights. The existing quote
interface remains available to frozen clients. All 42 tests pass.

Public-source leakage checks now also recognize bare DOI strings and DOI
values inside JSON manifests, not just labeled `DOI:` prose or doi.org links.
Both the runtime compatibility module and public validator share that rule;
authored source evidence stays private. All 43 tests pass.

A second runtime recovery defect was confirmed by Iteration 007 S100: Harbor
reported AgentTimeoutError after the authoring deadline and safely archived
one partial package, but marked the failure non-retryable and skipped the
remaining configured attempt. A completed single-trial timeout is now eligible
for the existing recovery path only when frozen inputs are intact, exactly one
safe partial package was collected, and retry budget remains. Arbitrary trial
exceptions, ambiguous artifacts and unsafe copies remain non-retryable. A
failed worker never gains readiness; the recovered draft must undergo another
construction attempt and all normal checks. All 44 tests pass.

The generated public data_sources manifest now omits all explicitly nonpublic
resources instead of publishing private reference/fixture metadata from the
internal execution registry. Public resources remain described, and private
resource declarations stay unchanged in the evaluator configuration. This
addresses the public-manifest boundary seen in live construction diagnostics.
All 45 tests pass.

A real span-based final T-P scoring request for the first task in the corrected
NatureBench random sample returned 33/40 with all eight criteria bound to exact
trajectory quotes. Complete T0 remains unknown because historical native
mechanical evidence is incomplete. The rest of the same sample is being
reviewed with bounded request concurrency; the first task was not selected by
its outcome or score.

Real construction traces continued to show unsupported paper claims and
experiment bindings while the authoring prompt demanded all files within five
tool turns. The prompt now establishes source-grounded objective, experiments,
resources, splits and metrics before a coherent draft, preserves the shipped
ABI scaffold, and uses configured runtime/attempt budgets instead of an
arbitrary corrective-turn cutoff. Scientific checks are not weakened.

The final process scorer now transmits all canonical ATIF behavior fields
for every step while omitting duplicate provider/usage telemetry. Messages,
reasoning text, tool arguments and observations are preserved without text
truncation; step counts and full original hashes are audited. Exact span
references still resolve against the original trajectory. This targets an
observed HTTP 400 on a long historical trace while preserving evidence.
All 46 tests pass.

A host/worker smoke discrepancy in Iteration 007 exposed a deterministic
framework defect. The framework copied the first independently authored
expected value onto every raw unit metric, even though smoke receipts expose
aggregation outputs. For example, a correct receipt with `mean_migration_f1`
and `mean_parsimony_ratio` was rejected after the framework added unreturned
`migration_f1`/`parsimony_ratio` expectations; the latter could also inherit an
unrelated scientific value. This inference is removed. Vocabulary/path
normalization preserves all authored expected metrics exactly, including
different units and values, across repeated normalization. Existing failed
packages and evidence remain unchanged. Regression and full suite: 46 pass.
This candidate fix precedes Iteration 008 worker snapshots; Iteration 007
continues with its frozen program. It does not reinterpret runtime errors as
valid rejections or remove any independent correctness controls.

Inspection of the actual S013 repair trace shows repeated attempts to remove
the generated `Submit` bullet to cure a deliverables-table error. The validator
actually found zero canonical artifact-table rows: the worker had replaced the
scaffold with a two-column prose table. The same symptom occurred in five
completed packages. Framework generation now inserts its output-table slot
under an existing Output heading when neither a slot nor a canonical table
exists, then renders only already-declared submission artifacts. Authored
scientific prose remains intact. Existing canonical tables are preserved and
conflicting format/field/count declarations still fail validation. This
removes a Markdown-layout obstacle without inventing deliverables or changing
scientific scope. Direct-file and archive-pattern regression checks plus the
full suite pass (48 tests). Iteration 008 has not yet taken worker snapshots.
