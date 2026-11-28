# Iteration 011

Date: 2026-11-28 (Asia/Shanghai)

This iteration follows two real Stage3 failures from Iteration 010. S030 had a
bundled JSON resource whose declared item count did not match its bytes; it
passed construction because scope-v3 treated every formal input as deferred.
S018 had a schema-compatible sample directory, while its runtime preparation
recipe was never scheduled because the resource-delivery classifier recognized
only commands containing `/task/problem/data`.

The general fix is in `31b39ce`: bundled formal resources are validated during
Stage2 even under scope-v3, and every required `runtime_download` or
`preprovided` resource with a valid preparation recipe is marked for Stage3
materialization. Existing samples are schema controls only; they do not prove
formal split identity. The implementation and legacy compatibility tree are
kept synchronized. Five focused resource tests plus the full 68-test suite
pass.

The new fixed-pool sample uses seed `20261202` and the unchanged 100-task pool:

`S022, S079, S040, S002, S094, S017, S065, S031, S070, S039`.

Selection occurred before readiness filtering. All ten have an immutable Stage1
source binding in the run artifact; the original denominator remains ten and
no case is replaced when preparation or construction fails. The run will use
four concurrent Stage2 constructors, one serial external Q/D grading stream,
and serial Stage3 Solver execution for construction-ready packages. Main is not
promoted without complete comparable Q/D/T evidence.

