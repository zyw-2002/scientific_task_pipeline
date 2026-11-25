"""Standalone Stage1 defaults; CLI/config orchestration may override them."""
from __future__ import annotations
import json
import os
from pathlib import Path
from copy import deepcopy

DEFAULTS = {
    'data_mode': 'sample', 'sample_files': 3, 'sample_rows': 100,
    'sample_file_mb': 10, 'sample_resource_mb': 20,
    'metadata_max_mb': 50, 'provider_cooldown_sec': 5,
    'github_min_interval': 1.0, 'metadata_cache_ttl_sec': 86400,
    'metadata_backoff_max_sec': 60, 'stage1_env_file': '',
}


def configuration():
    path=os.environ.get('SCIENTIFIC_PIPELINE_STAGE1_CONFIG')
    if not path: return deepcopy(DEFAULTS)
    value=json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value,dict): raise ValueError('Stage1 config must be an object')
    result=deepcopy(DEFAULTS); result.update(value); return result


def setting(name):
    return deepcopy(configuration().get(name))
