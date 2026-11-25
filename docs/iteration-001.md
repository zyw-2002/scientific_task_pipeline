# Iteration 001

Date: 2026-11-25 (Asia/Shanghai)

This iteration adds a reproducible ten-task sampling and evidence-aware preflight layer. The complete 100-task pool is sampled before readiness filtering, so missing data cannot bias selection toward easy cases. Each selected task retains its ID, source row, pool hash, selection seed, package path, data evidence, and any public URLs found in the task's own instructions.

The first recorded seed is `20261125`; the selected task IDs are `S002, S041, S058, S072, S032, S005, S067, S051, S024, S086`. The frozen 100-row pool hash is `812ea65046993e5f5f33499ce60d6fb1d030308088e8d21efbb34fb9395c4ef4`. Preflight found 3 tasks with non-empty local data evidence, 1 task with a public source URL suitable for a download attempt, and 6 tasks whose definitions exist without verifiable data evidence. These are coverage findings, not scientific scores.

The three legacy directories with non-empty data evidence were passed to the current Stage3 preparation boundary. All three were rejected because their historical directories do not contain the current pipeline's `execution.json`. This is recorded as a package-contract incompatibility; the old Harbor artifacts are not promoted to scored results.

The rubric remains the existing task rubric and Stage3 validity evidence. A missing or blocked task keeps a `null` score, and a ten-task iteration is successful only with 10/10 valid scores and mean score >= 90. No task-specific replacement, BBBP fallback, synthetic data, or verifier bypass was introduced.

Artifacts:

- `runs/iterations/iteration-001/selection.json` (ignored runtime artifact; reproducible from the seed and pool hash)
- `runs/iterations/iteration-001/preflight.json` (ignored runtime artifact; evidence and readiness classification)
- `src/pipeline/orchestration/iteration.py` and `bin/benchmark_iteration` (tracked implementation)
