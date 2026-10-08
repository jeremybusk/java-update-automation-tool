#!/usr/bin/env python3
"""Modernize one source repository and publish to an existing destination branch."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import java_migrator as engine
from java_update_tool.core import PortfolioError, STAGES, load_config, write_json
from java_update_tool.policy import validate_location, workflow_policy
from java_update_tool.runs import git
from java_update_tool.workflow import main as run_workflow


def location(value: str) -> str:
    validate_location(value)
    return value if engine.is_remote(value) else str(Path(value).expanduser().resolve())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='source Git URL or local directory')
    parser.add_argument('--destination', required=True, help='existing destination Git URL or local bare repository')
    parser.add_argument('--branch', required=True, help='destination branch to receive the validated commit')
    parser.add_argument('--source-ref', help='source branch, tag, or commit; defaults to its default branch')
    parser.add_argument('--java', type=int, choices=engine.TARGETS, help='override the policy Java target')
    parser.add_argument('--boot', help='override the Spring Boot target, e.g. 4.0.x')
    parser.add_argument('--profile', choices=[name for name in engine.PROFILE_DEFAULTS if name != 'report-only'], help='override the migration profile')
    parser.add_argument('--provider', choices=('github', 'gitlab'), help='destination credential provider')
    parser.add_argument('--config', type=Path, default=ROOT / 'java-update.yml', help='optional advanced policy')
    parser.add_argument('--state', type=Path, default=Path('.java-update'), help='retained runs in the calling directory')
    parser.add_argument('--execute', action='store_true', help='migrate, validate, and publish; otherwise plan only')
    parser.add_argument('--local-only', action='store_true', help='publish only the retained local checkout for testing')
    parser.add_argument('--resume', metavar='RUN_ID', help='retry a saved run with the same arguments; accepts latest')
    args = parser.parse_args(argv)
    try:
        source, destination = location(args.source), location(args.destination)
        git(Path.cwd(), 'check-ref-format', '--branch', args.branch)
        policy = args.config.expanduser().resolve()
        config = load_config(policy)
        for name, target in (('java', args.java), ('spring_boot', args.boot)):
            if target is not None:
                config['targets'][name] = {'desired': target, 'acceptable': [target]}
        migration = config.setdefault('migration', {})
        if args.profile:
            migration['profile'] = args.profile
        rewrite = migration.setdefault('openrewrite', {})
        for field in ('source_lock', 'source_cache'):
            if rewrite.get(field):
                path = Path(rewrite[field]).expanduser()
                rewrite[field] = str(path if path.is_absolute() else (policy.parent / path).resolve())
        workflow = config.setdefault('workflow', {})
        workflow.update(mode='unattended', include_dependencies=False, allow_dependency_override=False,
                        dependency_evidence={})
        publishing = workflow.setdefault('publishing', {})
        publishing.update(enabled=not args.local_only, mode='precreated',
                          targets=['local_repo'] if args.local_only else ['local_repo', 'dst_repo'],
                          default_branch=args.branch, repositories={'app': {'url': destination}})
        if args.provider:
            publishing['provider'] = args.provider
        workflow_policy(config)
        portfolio = {'schema_version': 1, 'repositories': [{
            'repo_name': 'app', 'source': source, 'ref': args.source_ref,
            'application_id': 'app', 'application_group_id': 'app',
        }]}
        # Stable, retained inputs allow retries without a user-maintained portfolio file.
        payload = json.dumps({'config': config, 'portfolio': portfolio}, sort_keys=True)
        state = args.state.expanduser().resolve()
        inputs = state / 'inputs' / hashlib.sha256(payload.encode()).hexdigest()[:20]
        write_json(inputs / 'config.json', config)
        write_json(inputs / 'repositories.json', portfolio)
        command = ['--config', str(inputs / 'config.json'), '--portfolio', str(inputs / 'repositories.json'),
                   '--state', str(state), 'run', '--all', '--through', STAGES[5] if args.execute else STAGES[2]]
        if args.execute:
            command.append('--execute')
        if args.resume:
            command += ['--resume', args.resume]
        return run_workflow(command)
    except (PortfolioError, OSError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
