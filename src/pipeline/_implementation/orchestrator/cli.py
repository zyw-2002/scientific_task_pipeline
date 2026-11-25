#!/usr/bin/env python3
"""Explicit command-line experiment configuration for Stage1 -> Stage4."""
from __future__ import annotations
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


class ConfiguredParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        parsed = super().parse_args(args, namespace)
        if self.load_defaults:
            from copy import deepcopy
            from settings import configuration
            defaults = configuration()
            if getattr(parsed, 'config', None):
                import json
                from copy import deepcopy
                profile = json.loads(parsed.config.read_text(encoding='utf-8'))
                if not isinstance(profile, dict):
                    raise ValueError('config must contain an object of pipeline settings')
                known = {action.dest for action in self._actions}
                unknown = set(profile) - known - {'input_fingerprints', 'package_bindings_sha256'}
                if unknown:
                    raise ValueError('unknown config keys: ' + ', '.join(sorted(unknown)))
                for action in self._actions:
                    if action.dest in profile and action.dest not in {'config', 'save_config', 'plan', 'resume', 'print_config', 'print_guide'}:
                        value = profile[action.dest]
                        if value is not None and action.type:
                            value = [action.type(v) for v in value] if isinstance(value, list) else action.type(value)
                        if action.choices and value is not None:
                            selected = value if isinstance(value, list) else [value]
                            if any(item not in action.choices for item in selected):
                                raise ValueError('invalid saved setting: ' + action.dest)
                        defaults[action.dest] = value
            for action in self._actions:
                current = getattr(parsed, action.dest, None)
                if action.dest in defaults and (current is None or isinstance(action, argparse._AppendAction) and not current):
                    setattr(parsed, action.dest, deepcopy(defaults[action.dest]))
                    inherited = {'judge_api_key_env':'api_key_env', 'analysis_api_key_env':'api_key_env',
                                 'build_proxy_url':'proxy_url'}.get(action.dest)
                    if inherited and defaults[action.dest] == defaults.get(inherited):
                        setattr(parsed, action.dest, getattr(parsed, inherited))
        return parsed


def parser(load_defaults=True):
    p = ConfiguredParser(description=__doc__)
    p.load_defaults = load_defaults
    p.add_argument('--config', type=Path, default=None, help='Load a saved parameter profile; explicit CLI flags override it')
    p.add_argument('--save-config', type=Path, default=None, help='Save effective parameters without executing; refuse overwrite')
    p.add_argument('--guide', dest='print_guide', action='store_true', help='Print the local launcher guide without execution')
    p.add_argument('--output-root', type=Path, default=None)
    p.add_argument('--output-layout', choices=['compact','legacy'], default=None)
    p.add_argument('--batch-id', default=None)
    p.add_argument('--timezone', default=None)
    p.add_argument('--python', default=None)
    p.add_argument('--plan', action='store_true', help='Write expanded plan only; no data/model/container execution')
    p.add_argument('--resume', action='store_true')
    p.add_argument('--print-config', action='store_true', help='Print Bash-derived settings without executing any stage')
    p.add_argument('--stop-after', choices=['stage1','stage2','stage3','stage4'], default=None)
    p.add_argument('--on-error', choices=['continue','stop'], default=None)
    p.add_argument('--command-timeout-sec', type=int, default=None)
    p.add_argument('--records', type=Path, help='Existing enriched records: skip Stage1 01-09')
    p.add_argument('--repair-build', type=Path, help='Use one matching previous Stage2 build.json and its checks as a draft for a new 01/02 run')
    p.add_argument('--source-task-instruction', type=Path,
                   help='Solver-visible scientific specification of the fixed task to reconstruct for a single paper')
    p.add_argument('--download-results', type=Path, help='Existing completed download index: skip Stage1 10')
    p.add_argument('--package', type=Path, help='Verified task package: skip Stage1/2 and run controlled experiments')
    p.add_argument('--package-bindings', type=Path, help='Verified case JSONL: sample_id, experiment, variant, readiness_status=ready, verified_package_path; use each fixed variant')
    p.add_argument('--run-manifest', type=Path, action='append', default=[], help='Existing Stage3 result; run only Stage4')
    p.add_argument('--jina-xlsx', type=Path, default=None)
    p.add_argument('--test-xlsx', type=Path, default=None)
    p.add_argument('--manual-csv', type=Path, default=None)
    p.add_argument('--stage1-env-file', type=Path, default=None)
    p.add_argument('--jina-runs-root', type=Path, default=None)
    p.add_argument('--source-types', default=None)
    p.add_argument('--sample-domains', default=None)
    p.add_argument('--difficulty-counts', default=None)
    p.add_argument('--review-policy', choices=['title','balanced'], default=None)
    p.add_argument('--paper-id', action='append', default=[])
    p.add_argument('--stage1-record-limit', type=int, default=None)
    p.add_argument('--max-papers', type=int, default=None)
    p.add_argument('--year-min', type=int, default=None)
    p.add_argument('--year-max', type=int, default=None)
    p.add_argument('--unknown-year', choices=['include','exclude'], default=None)
    p.add_argument('--experiment-id', default=None)
    p.add_argument('--domain', choices=['auto','general','physics','geography','chemistry','biology','materials','astronomy'], default=None)
    p.add_argument('--seed', type=int, default=None)
    p.add_argument('--download-workers', type=int, default=None)
    p.add_argument('--search-workers', type=int, default=None)
    p.add_argument('--google-search-workers', type=int)
    p.add_argument('--scholar-search-workers', type=int)
    p.add_argument('--search-timeout', type=float)
    p.add_argument('--search-retries', type=int)
    p.add_argument('--search-delay', type=float)
    p.add_argument('--search-progress-seconds', type=float)
    p.add_argument('--download-timeout', type=float, default=None)
    p.add_argument('--download-retries', type=int, default=None)
    p.add_argument('--max-file-mb', type=int, default=None)
    p.add_argument('--max-total-gb', type=float, default=None)
    p.add_argument('--article-max-mb', type=int, default=None)
    p.add_argument('--max-page-urls', type=int, default=None)
    p.add_argument('--max-pdf-urls', type=int, default=None)
    p.add_argument('--max-fallback-page-urls', type=int, default=None)
    p.add_argument('--max-jina-fallbacks', type=int, default=None)
    p.add_argument('--doi-fallback', action=argparse.BooleanOptionalAction, default=None)
    p.add_argument('--jina-fallback', action=argparse.BooleanOptionalAction, default=None)
    p.add_argument('--url-check', choices=['none','head'], default=None)
    p.add_argument('--enrichment-sources', default=None)
    p.add_argument('--search-profile', choices=['balanced','thorough'], default=None)
    p.add_argument('--max-search-queries', type=int, default=None)
    p.add_argument('--graph-rounds', type=int, default=None)
    p.add_argument('--graph-pages', type=int, default=None)
    p.add_argument('--graph-nodes', type=int, default=None)
    p.add_argument('--graph-page-workers', type=int)
    p.add_argument('--graph-timeout', type=float)
    p.add_argument('--graph-enabled', action=argparse.BooleanOptionalAction)
    p.add_argument('--quality-samples', type=int, default=None)
    p.add_argument('--data-mode', choices=['sample','metadata-only'], default=None)
    p.add_argument('--sample-files', type=int, default=None)
    p.add_argument('--sample-rows', type=int, default=None)
    p.add_argument('--sample-file-mb', type=int, default=None)
    p.add_argument('--sample-resource-mb', type=int, default=None)
    p.add_argument('--github-min-interval', type=float, default=None)
    p.add_argument('--metadata-cache-ttl-sec', type=float)
    p.add_argument('--metadata-max-mb', type=int)
    p.add_argument('--provider-cooldown-sec', type=float)
    p.add_argument('--metadata-backoff-max-sec', type=float)
    p.add_argument('--link-page-max-bytes', type=int)
    p.add_argument('--transfer-chunk-bytes', type=int)
    p.add_argument('--watch-poll-seconds', type=float)
    p.add_argument('--watch-report-seconds', type=float)
    p.add_argument('--max-deferred-retries', type=int)
    p.add_argument('--jina-interval-with-key', type=float)
    p.add_argument('--jina-interval-without-key', type=float)
    p.add_argument('--stage1-action', choices=['pipeline','search','download'])
    p.add_argument('--stream-records', type=Path)
    p.add_argument('--stream-search-input', type=Path)
    p.add_argument('--stream-open-source-dir', type=Path)
    p.add_argument('--stream-search-dir', type=Path)
    p.add_argument('--stream-download-dir', type=Path)
    p.add_argument('--stream-search-cache', type=Path)
    p.add_argument('--stream-all-records', action=argparse.BooleanOptionalAction)
    p.add_argument('--stream-retry-failed', action=argparse.BooleanOptionalAction)
    p.add_argument('--stream-retry-only-failed', action=argparse.BooleanOptionalAction)
    p.add_argument('--stream-once', action=argparse.BooleanOptionalAction)
    p.add_argument('--proxy-url', default=None)
    p.add_argument('--build-proxy-url', default=None)
    p.add_argument('--no-proxy', default=None)
    p.add_argument('--probe-url', action='append', default=[])
    p.add_argument('--gateway-config', type=Path)
    p.add_argument('--material-root', type=Path, action='append', default=[])
    p.add_argument('--multimodal', action=argparse.BooleanOptionalAction, default=None)
    p.add_argument('--model-capability', choices=['text','vision','unknown'], default=None)
    p.add_argument('--harbor', default=None)
    p.add_argument('--docker', default=None)
    p.add_argument('--base-image', default=None)
    p.add_argument('--solver-proxy-url', default=None, help='Download proxy for public-network Stage3 Solver; does not enable restricted networks')
    p.add_argument('--task-image', default=None, help='Prebuilt Solver/evaluator image bound to Stage3 run copies')
    p.add_argument('--model-env-file', type=Path, default=None)
    p.add_argument('--model-base-url', default=None)
    p.add_argument('--api-key-env', default=None)
    p.add_argument('--construct-model', default=None)
    p.add_argument('--verify-model', default=None)
    p.add_argument('--construct-endpoint', default=None)
    p.add_argument('--verify-endpoint', default=None)
    p.add_argument('--worker-agent', default=None)
    p.add_argument('--worker-cpus', type=int, default=None)
    p.add_argument('--worker-memory-mb', type=int, default=None)
    p.add_argument('--worker-gpus', type=int, default=None)
    p.add_argument('--construct-timeout-sec', type=int, default=None)
    p.add_argument('--verify-timeout-sec', type=int, default=None)
    p.add_argument('--build-timeout-sec', type=int, default=None)
    p.add_argument('--max-construct-attempts', type=int, default=None)
    p.add_argument('--max-verify-attempts', type=int, default=None)
    p.add_argument('--probe-timeout-sec', type=int, default=None)
    p.add_argument('--model', action='append', default=[])
    p.add_argument('--agent', default=None)
    p.add_argument('--model-endpoint', action='append', default=[], metavar='MODEL=URL')
    p.add_argument('--model-agent', action='append', default=[], metavar='MODEL=AGENT')
    p.add_argument('--model-api-key-env', action='append', default=[], metavar='MODEL=ENV_NAME')
    p.add_argument('--agent-kwarg', action='append', default=[], metavar='NAME=JSON')
    p.add_argument('--variant', action='append', choices=['blind','guided'], default=[])
    p.add_argument('--rollouts', type=int, default=None)
    p.add_argument('--run-workers', type=int, default=None)
    p.add_argument('--stage2-workers', type=int, default=None,
                   help='Maximum number of independent Stage2 construction workers')
    p.add_argument('--scenario', action='append', default=[], help='canonical, attachment, or a reviewed scenario JSON path')
    p.add_argument('--attempt-policy', choices=['latest-scored','explicit'], default=None)
    p.add_argument('--attempt-id')
    p.add_argument('--judge-model', default=None)
    p.add_argument('--judge-endpoint', default=None)
    p.add_argument('--judge-api-key-env', default=None)
    p.add_argument('--judge-timeout-sec', type=int, default=None)
    p.add_argument('--judge-max-input-bytes', type=int, default=None)
    p.add_argument('--judge-max-output-tokens', type=int, default=None)
    p.add_argument('--judge-temperature', type=float, default=None)
    p.add_argument('--analysis-mode', choices=['review','metrics-only'], default=None)
    p.add_argument('--analysis-model', default=None)
    p.add_argument('--analysis-endpoint', default=None)
    p.add_argument('--analysis-api-key-env', default=None)
    p.add_argument('--analysis-timeout-sec', type=float, default=None)
    p.add_argument('--analysis-max-output-tokens', type=int, default=None)
    p.add_argument('--analysis-max-input-bytes', type=int, default=None)
    p.add_argument('--pricing-json', type=Path)
    p.add_argument('--prompt01', type=Path, default=None)
    p.add_argument('--prompt02', type=Path, default=None)
    p.add_argument('--prompt03', type=Path, default=None)
    p.add_argument('--prompt04', type=Path, default=None)
    p.add_argument('--analysis-prompt', type=Path, default=None)
    return p


def main(argv=None):
    import json
    try:
        args = parser().parse_args(argv)
        if args.print_guide:
            print((ROOT / 'RUN_PIPELINE_GUIDE.md').read_text(encoding='utf-8'))
            return 0
        if args.save_config:
            values = {key: value for key, value in vars(args).items() if key not in
                      {'config', 'save_config', 'plan', 'resume', 'print_config', 'print_guide'}}
            args.save_config.parent.mkdir(parents=True, exist_ok=True)
            with args.save_config.open('x', encoding='utf-8') as stream:
                json.dump(values, stream, ensure_ascii=False, indent=2, default=str)
                stream.write('\n')
            print(json.dumps({'status': 'saved', 'config': str(args.save_config.resolve()), 'execution_started': False}))
            return 0
        if args.print_config:
            print(json.dumps(vars(args), ensure_ascii=False, indent=2, default=str))
            return 0
    except (OSError, ValueError, TypeError) as error:
        print(json.dumps({'status':'failed','error':str(error)},ensure_ascii=False),file=sys.stderr)
        return 1
    if args.stage1_action != 'pipeline':
        from live_stage1 import launch
        return launch(args, ROOT)
    from runner import run
    try:
        result = run(args, ROOT)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['status'] in {'completed','planned','stopped'} else 2
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        print(json.dumps({'status':'failed','error':str(error)},ensure_ascii=False),file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
