# Prompt interface

The files in this directory are source-controlled prompt contracts. Host-side source paths are intentionally absent from the prompts.

Stage2 workers always receive the following stable container interface:

- `/task/input/paths.json`
- `/task/input/pipeline_tools/`
- `/task/input/source_bundle/`
- `/task/input/source_inventory.json`
- `/task/input/fixed_task_instruction.md` when supplied
- writable `paths.output_dir`

`pipeline_tools/` contains compatibility command wrappers. The wrappers keep commands such as
`python /task/input/pipeline_tools/preflight.py` stable while delegating to `pipeline.*` modules.

Stage3 solver and judge prompts use only the runtime interfaces `/task/problem`, `/workspace`, and
request/evidence objects. They must not refer to the host repository layout.
