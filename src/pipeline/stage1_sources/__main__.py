from .runner import run_step
import argparse

p=argparse.ArgumentParser()
p.add_argument('step', choices=sorted(__import__('pipeline.stage1_sources.runner', fromlist=['STEP_FILES']).STEP_FILES))
p.add_argument('args', nargs=argparse.REMAINDER)
args=p.parse_args()
raise SystemExit(run_step(args.step, args.args))
