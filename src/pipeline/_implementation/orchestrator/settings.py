"""Standalone defaults for the reorganized pipeline implementation."""
from __future__ import annotations
from copy import deepcopy
import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULTS = {
    'max_construct_attempts': 0, 'max_verify_attempts': 2,
    'probe_timeout_sec': 300, 'docker': 'docker',
    'variant': ['blind'], 'model': [''], 'agent': 'qwen-coder', 'harbor': 'harbor',
    'judge_model': '', 'judge_endpoint': '', 'judge_api_key_env': 'OPENAI_API_KEY',
    'judge_timeout_sec': 300, 'judge_max_input_bytes': 8_000_000,
    'judge_temperature': 0.0, 'judge_max_output_tokens': 12_000,
    'api_key_env': 'OPENAI_API_KEY', 'model_env_file': '', 'model_base_url': '',
    'attempt_policy': 'latest-scored',
    'prompt03': str(PROJECT_ROOT / 'prompts/stage3/03_solver_instruction.md'),
    'prompt04': str(PROJECT_ROOT / 'prompts/stage3/04_run_verify.md'),
}


def configuration():
    path = os.environ.get('SCIENTIFIC_PIPELINE_CONFIG') or os.environ.get('NATUREBENCH_PIPELINE_CONFIG')
    if not path:
        return deepcopy(DEFAULTS)
    value=json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict): raise ValueError('pipeline config must contain an object')
    result=deepcopy(DEFAULTS); result.update(value); return result


def value(name):
    values=configuration()
    if name not in values: raise ValueError(f'missing pipeline configuration: {name}')
    return deepcopy(values[name])


def set_stage_defaults(parser, mapping):
    parser.set_defaults(**{argument: value(setting) for argument, setting in mapping.items()})
    return parser
