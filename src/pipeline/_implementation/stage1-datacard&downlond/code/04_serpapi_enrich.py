#!/usr/bin/env python3
"""Compatibility entry point for the stage-07 SerpAPI fallback."""

from pathlib import Path
import runpy


runpy.run_path(str(Path(__file__).with_name("07_serpapi_fallback.py")), run_name="__main__")
