# Iteration 002

Date: 2026-11-25 (Asia/Shanghai)

This revision clarifies the Stage2 resource gate. A required dataset, label set, checkpoint, or target that is absent from the construction package may remain a construction `PASS` when the task declares an authorized source plus runnable `prepare.command` and independent `prepare.validate`. Such a task is marked `execution_ready=false` until Stage3 materializes and verifies the resource. `BLOCKED` is reserved for a required resource with no authorized source or no runnable verification path. This keeps construction general across task domains and leaves acquisition/installation to the runtime contract.

The clarification was applied to both the tracked Stage2 prompt and the implementation snapshot used by the worker, including the verifier prompt. It does not introduce a fallback dataset or modify any task case.

Validation evidence:

- Current Stage3 `prepare` succeeded for S032, S067, and S086 from existing verified packages; their dry-run workflows completed and froze effective inputs/manifests without calling an external model.
- S002 was rejected by the current Stage3 preparation boundary as `REWORK`, which is retained as a package-contract result rather than converted into a score.
- A real external Solver attempt was not executed because the environment approval layer rejected sending the selected task package and authenticated interaction to the configured external LLM endpoint/proxy. No score is claimed from that attempt.
