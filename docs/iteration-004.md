# Iteration 004

Date: 2026-11-26 (Asia/Shanghai)

This revision studies the existing trajectory-analysis system and two related
benchmark stores before changing the pipeline. The previous
`analysis_tractory` system contributes deterministic ATIF accounting and an
optional F1--F8/ENV semantic review. Its semantic labels remain diagnostic;
they are not silently converted into a universal score.

The fixed v1 contract is documented in `docs/rubric-tqd-v1.md`: Q has six
task-quality dimensions, D has five objective-difficulty dimensions, and T
has a 60-point execution-evidence layer plus a 40-point evidence-bound
semantic-process layer. `ENV` remains a separate environment status.

The pipeline now records three separate namespaces:

* Q0/task quality: the existing six Stage2 construction dimensions;
* T0/trajectory evidence: observable execution evidence with explicit
  `observed_score`, `coverage`, and `unknown_dimensions`;
* scientific: Stage3 evaluator metric, validity, and Judge result.

T0 does not fill unknown evidence with zero. A native Stage3 result receives
positive points only for linked input/run/export/freeze evidence, passing
contract evidence, real execution, independent validation, and an accepted
completion claim. ATIF trajectories from external benchmark stores use a
conservative structural adapter; task-contract dimensions remain unknown.
F1--F8/ENV reviews can be attached with exact-quote and review-status fields,
but only human-verified semantic reviews are eligible to contribute to a
future calibrated semantic score.

The Solver-visible boundary now rejects DOI/article URLs, source accessions,
GitHub URLs and commit hashes, private authoring paths, recognizable paper
citation patterns, and original success-result claims in `instruction.md`,
public README/data descriptions, and public manifests. Authoring/review
evidence remains outside this scan. Stage2 prompts state the same rule and
allow only abstract methods plus authorized, independently validated resource
preparation.

Offline cross-benchmark audit:

* NatureBench best-manifest: 91 selected trials, 91 trajectories present, 90
  structurally assessed; no complete T0 score was claimed because contract and
  input-identity evidence are unknown.
* ScientistWorld-prepared: 62 packages, no main solver trajectories; all are
  recorded as `unscored_no_solver_trajectory`, never as zero.
* An old S032 package was checked with the new Stage2 preflight. The new gate
  caught public GitHub/source-commit leakage in `data_sources.json` and
  `README.md`, alongside pre-existing contract/resource defects. This confirms
  that the boundary check is active on real package content.

The Q0 baseline remains the prior authorized result (mean 93.25/100 over the
fixed ten tasks). No external Solver or rubric judge was called in this code
revision, so there is no claim yet that the Q0/scientific score improved. The
next authorized scoring batch must use a fresh random sample from the 100-task
pool and report Q0, T0 coverage, and scientific score separately.
