"""Access to the single configuration source, run_pipeline.sh."""
import importlib.util
from pathlib import Path
import sys

_name = '_naturebench_pipeline_settings'
if _name not in sys.modules:
    _path = Path(__file__).resolve().parent / 'orchestrator/settings.py'
    _spec = importlib.util.spec_from_file_location(_name, _path)
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_name] = _module
    _spec.loader.exec_module(_module)
_settings = sys.modules[_name]
setting = _settings.value
stage_defaults = _settings.set_stage_defaults
