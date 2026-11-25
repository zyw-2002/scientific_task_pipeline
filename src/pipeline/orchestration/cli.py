"""Small top-level dispatcher for the reorganized three-stage pipeline."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from pipeline.stage1_sources.runner import STEP_FILES


def build_parser() -> argparse.ArgumentParser:
    parser=argparse.ArgumentParser(description='Scientific task pipeline dispatcher')
    sub=parser.add_subparsers(dest='stage', required=True)
    s1=sub.add_parser('stage1', help='run one Stage1 source step')
    s1.add_argument('step', choices=sorted(STEP_FILES))
    s1.add_argument('args', nargs=argparse.REMAINDER)
    s2=sub.add_parser('stage2', help='run Stage2 construction CLI')
    s2.add_argument('args', nargs=argparse.REMAINDER)
    s3=sub.add_parser('stage3', help='run Stage3 execution CLI')
    s3.add_argument('args', nargs=argparse.REMAINDER)
    plan=sub.add_parser('plan', help='print the clean output layout without executing a stage')
    plan.add_argument('--root', type=Path, default=Path.cwd())
    return parser


def main(argv=None) -> int:
    args=build_parser().parse_args(argv)
    if args.stage == 'plan':
        root=args.root.resolve()
        print(json.dumps({'status':'planned','root':str(root),
          'inputs':str(root/'inputs'),'work':str(root/'work'),
          'packages':str(root/'packages'),'runs':str(root/'runs'),
          'stages':['stage1_sources','stage2_construction','stage3_execution']}, indent=2))
        return 0
    if args.stage == 'stage1':
        from pipeline.stage1_sources.runner import run_step
        return run_step(args.step, args.args)
    if args.stage == 'stage2':
        from pipeline.stage2_construction.entrypoint import main as stage2_main
        return stage2_main(args.args)
    from pipeline.stage3_execution.entrypoint import pipeline_main
    return pipeline_main(args.args)
