"""Run isolated migration cases with bounded parallelism and CI timing summaries."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from integration_fixtures import CASES


def run_case(case: str, environment: dict[str, str], logs: Path) -> tuple[str, int, float]:
    env = {**environment, 'JAVA_UPDATE_INTEGRATION': '1', 'JAVA_UPDATE_CASE': case}
    log = logs / f'{case}.log'
    started = time.monotonic()
    with log.open('w') as output:
        result = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests',
                                 '-p', 'test_integration.py', '-v'], cwd=ROOT, env=env,
                                stdout=output, stderr=subprocess.STDOUT)
    elapsed = time.monotonic() - started
    print(f'{case}: exit {result.returncode}, {elapsed:.1f}s; log: {log}', flush=True)
    if result.returncode:
        print(log.read_text(), flush=True)
    return case, result.returncode, elapsed


def summary(rows: list[tuple[str, int, float]]) -> None:
    lines = ['## Migration timings', '', '| Check | Result | Seconds |', '| --- | --- | ---: |']
    lines.extend(f'| {name} | {"passed" if code == 0 else "failed"} | {elapsed:.1f} |'
                 for name, code, elapsed in rows)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a') as output:
            output.write('\n'.join(lines) + '\n')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cases', nargs='+', choices=CASES)
    parser.add_argument('--jobs', type=int, default=1)
    parser.add_argument('--prepare-recipes', action='store_true')
    args = parser.parse_args(argv)
    if args.jobs < 1 or len(set(args.cases)) != len(args.cases):
        parser.error('jobs must be positive and cases must be unique')
    env = os.environ.copy()
    if not env.get('JAVA_UPDATE_ARTIFACTS'):
        env['JAVA_UPDATE_ARTIFACTS'] = tempfile.mkdtemp(prefix='java-update-ci-')
    logs = Path(env['JAVA_UPDATE_ARTIFACTS']) / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    rows = []
    if args.prepare_recipes:
        config = yaml.safe_load((ROOT / 'java-update.yml').read_text())
        if config['migration']['openrewrite'].get('recipe_repository') in {'source', 'auto'}:
            started = time.monotonic()
            result = subprocess.run([sys.executable, '-B', 'scripts/build_recipe_sources.py'], cwd=ROOT, env=env)
            rows.append(('recipe preparation', result.returncode, time.monotonic() - started))
            if result.returncode:
                summary(rows)
                return result.returncode
    with ThreadPoolExecutor(max_workers=min(args.jobs, len(args.cases))) as pool:
        futures = [pool.submit(run_case, case, env, logs) for case in args.cases]
        rows.extend(future.result() for future in futures)
    summary(rows)
    return 1 if any(code for _, code, _ in rows) else 0


if __name__ == '__main__':
    sys.exit(main())
