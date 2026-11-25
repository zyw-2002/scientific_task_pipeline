# Scientific Background

Measurements of a physical quantity can be expensive even when inexpensive covariates are available for many samples. A predictive model can estimate the quantity for unmeasured samples, but its usefulness depends on evaluating predictions on observations that were not used to fit the model. In this illustrative task, every sample has a stable identifier, numerical covariates and a common measurement unit.

# Research Objective

Predict the measured quantity for the held-out samples. Compare plausible approaches using the provided validation split, then submit one finite numerical prediction for every test sample. The task evaluates predictive accuracy; it does not ask you to establish a causal relationship between a covariate and the measured quantity.

# Task and Deliverables

Inspect the data descriptions, implement and validate a prediction method, and produce the final prediction table. Preserve sample identities and the supplied split. You may choose any method compatible with the provided inputs and execution environment. No particular model architecture is required.

# Input

The data directory contains observations.csv with labeled training and validation samples, and test.csv with test sample IDs and covariates. The split column in observations.csv identifies the allowed training and validation rows. Target values use the measurement unit documented in data_description.md. Test target values are hidden from the solver and used only by the evaluator.

# Output

Submit a CSV file with a header and exactly one prediction for every ID in test.csv, in that file's row order. The prediction column must contain finite numbers in the same unit as the target; sample_id must match the provided identifier exactly. Extra or missing IDs invalidate the submission. For example, a row for sample A17 with an estimated value of 2.4 is A17,2.4.

| File or pattern | Count | Format | Fields |
|---|---|---|---|
| /workspace/output/predictions.csv | 1 | csv | sample_id, prediction |

| Setting | Value |
|---|---|
| Submission path | /workspace/output/predictions.csv |

# Constraints

Use only the supplied observations and covariates. Do not modify the split or use test labels. Any transformations fitted to data must be fitted without access to held-out targets. The evaluator computes the score from the submitted prediction values; a score reported by the solver is not used for evaluation.

# Environment

The task uses the installed Python environment and declared dependencies. External networking and scientific searches are disabled; all required inputs must therefore be present before solving starts. This workload is assigned no GPUs, although the target hardware family for tasks requiring GPUs is NVIDIA A100.

| Setting | Value |
|---|---|
| CPU cores | 4 |
| Memory (MiB) | 16384 |
| GPUs | 0 |
| GPU model | NVIDIA A100 |
| Data directory | /task/problem/data |
| Network | disabled |
| Search | none |

# Evaluation

The score, named mae, is the mean absolute difference between predicted and observed target values over the test samples, with each sample weighted equally. Lower is better. The evaluator aligns predictions to the supplied sample IDs and computes the metric from the actual submitted bytes. A poor but well-formed prediction table remains valid and receives its measured error.

# Edge Cases

Missing IDs, duplicate IDs, extra rows and nonfinite predictions are invalid. Negative and constant predictions are not automatically invalid: their scientific plausibility depends on the documented quantity, and their quality is assessed by the metric. If the input documentation specifies a physical validity range, follow that stated range.

# Time Budget

Allow approximately one hour for understanding the inputs, implementing a method, validating it and generating the final submission. This estimate assumes the documented dependencies and inputs are available. The enforced total time limit is zero, meaning that no total deadline is set; the estimate is not an automatic stopping time.

| Setting | Value |
|---|---|
| Time limit (seconds) | 0 |
| Expected time (minutes) | 60 |
