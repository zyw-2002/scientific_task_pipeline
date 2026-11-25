#!/usr/bin/env python3
"""Compatibility entry point for the stage-08 final checker."""

from pathlib import Path
import runpy


runpy.run_path(str(Path(__file__).with_name("08_check_pipeline_results.py")), run_name="__main__")
