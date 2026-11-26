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

Real execution started on 2026-11-26 at 20:27 Asia/Shanghai. Seven workers
started immediately, with three slots reserved for the previous two Stage2
reviews and the ongoing Iteration 006 S022 Stage3 run. When those reviews
finished, two slots returned to the shared supervisor. The ten selected IDs
remain unchanged. Worker snapshots use the tested Stage2 revision `dd87bfb`;
grading-only changes below do not alter those snapshots. No candidate has been
promoted to main.

Iteration 007 is now terminal: no construction-ready package. Its preserved
query-only rubric returned Q0=99.444 and D0=67.014 for nine collected packages
(90% coverage). These figures neither establish a passing batch nor provide
Stage3/T evidence. S094 passed static checks, but its reviewer kept status
blocked solely because declared formal inputs require Stage3 preparation;
the launcher's default execution-readiness scope also asked Stage2 to enforce
runtime readiness. This boundary is being repaired on the next candidate,
without changing this batch's frozen Stage2 configuration.

A real retry of the long NatureBench process review returned HTTP 400 with
`Range of input length should be [1, 983616]`. The grading client now partitions
long behavior views into consecutive complete steps, externally extracts
evidence-bound observations for every segment, then makes one global F1--F8
judgment. It preserves all behavior text, audits complete step coverage,
retains the original trajectory hash, and checks every final quote. Segment
observations contain no point scores; recovery is judged globally rather
than by averaging segments. A single oversized step remains explicitly
unscored instead of being truncated. Actual string span IDs are normalized
to the same validated pointer objects; unknown IDs still fail. The first
segment's returned response was reused only after exact request/prompt hash
checks. Full test suite: 51 passing. The complete real review remains running.

The long-trajectory integration review has completed: all 195 original steps
were externally reviewed in seven ordered segments, followed by one global
judgment. All eight exact-bound criteria scored 5, giving T-P=40/40. This is
not T0=100: the historical trace still lacks independent native evidence for
the other mechanical dimensions. The same fixed NatureBench sample is now
being reviewed with the same client, keeping a malformed historical trace
unknown rather than replacing it.

The external ATIF adapter now accepts a hash/quote-bound semantic review and
recomputes both observed points and their maximum from the same dimensions.
This replaces earlier ad-hoc report augmentation that could add process
points while retaining a stale 12-point observed maximum. A 32/40 process
review plus 12/12 linked execution evidence correctly reports 44 observed
points out of 52 assessed, 52% dimensional coverage, and no complete T0.
Changed source bytes invalidate the process contribution. All 52 tests pass.

The ensuing fixed-sample NatureBench review exposed two response-format
defects: one invented span ID in an otherwise valid segment, and criterion
objects mistakenly nested under another criterion. Unique named criterion
objects can now be flattened without altering their observations; ambiguous
duplicates are rejected. A segment gets at most one external citation-only
repair after validation fails, independent of any score. Every observation
statement and its order must remain identical, otherwise the repair is
rejected. Unfinished reviews may resume only with identical archived model,
prompt and input hashes; complete reviews cannot be overwritten. Cache reuse
is audited and never selects a preferred score. All 54 tests pass.

The endpoint subsequently returned `insufficient_quota` / "Workspace allocated
quota exceeded, please increase your quota limit", followed by HTTP 403 on
some later requests. Grading and queued-launch supervisors were stopped to
avoid further unavailable requests. Actual Stage2/Stage3 child processes and
their outputs were preserved. `quota-checkpoint.json` records all ten selected
IDs and nine started immutable worker snapshots; S099 remains not started
because of the environment block, not removed from the batch. The unchanged
100-task pool was also preserved under the ignored runtime pool directory.
The next candidate is not launched while this endpoint remains unavailable.

The final grader now treats quota exhaustion as an explicit environment block
and immediately stops retrying it as ordinary HTTP 429 rate limiting. The
partial NatureBench review and unfinished grading ledgers remain available
for identity-checked continuation; absent grades are not scientific zeroes.
No complete Iteration 008 Q/D/T result or improvement is claimed.

Iteration summaries now prefer the selected `sample_id` when native Stage3
also supplies its own hashed execution `task_id`. This matches seed-pool
identity handling and prevents an otherwise valid native result being
misreported as a task replacement. Native execution identities remain intact.
Quota/no-retry and selected-ID regression checks bring the suite to 56 tests,
all passing.
