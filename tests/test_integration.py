"""Opt-in real toolchain migrations; never publish to hosted repositories."""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from java_update_tool.cli import main
from java_update_tool.core import STAGES, artifact_path, read_json
from java_update_tool.runs import git, run_directory
from java_update_tool.maintenance import export_evidence
from integration_fixtures import CASES, create


@unittest.skipUnless(os.environ.get('JAVA_UPDATE_INTEGRATION') == '1', 'set JAVA_UPDATE_INTEGRATION=1 for real toolchains')
class RealMigrationTests(unittest.TestCase):
    def migrate(self, case):
        artifact_root = os.environ.get('JAVA_UPDATE_ARTIFACTS')
        if artifact_root:
            directory = Path(artifact_root) / case
            directory.mkdir(parents=True, exist_ok=True)
            manager = contextlib.nullcontext(str(directory))
        else:
            manager = tempfile.TemporaryDirectory(prefix='java-update-integration-')
        with manager as temporary:
            root = Path(temporary)
            source = root / 'service'
            source.mkdir()
            tool, target, config = create(case, source)
            self.assertTrue(shutil.which('java') and shutil.which('mvn' if tool == 'maven' else 'gradle'), f'JDK and {tool} must be installed')
            git(source, 'init', '-b', 'master')
            git(source, 'add', '--all')
            git(source, 'commit', '-m', 'Original migration fixture')
            original = git(source, 'rev-parse', 'HEAD')
            portfolio = {'schema_version': 1, 'repositories': [{'repo_name': 'service', 'source': str(source),
                         'application_id': 'service', 'application_group_id': 'example'}]}
            config_path, portfolio_path, state = root / 'java-update.yml', root / 'repositories.yml', root / 'state'
            config_path.write_text(yaml.safe_dump(config))
            portfolio_path.write_text(yaml.safe_dump(portfolio))
            transcript = io.StringIO()
            with contextlib.redirect_stdout(transcript), contextlib.redirect_stderr(transcript):
                result = main(['--config', str(config_path), '--portfolio', str(portfolio_path), '--state', str(state),
                               'run', '--through', STAGES[5], '--execute'])
            (root / "transcript.txt").write_text(transcript.getvalue())
            if (state / 'latest.json').exists():
                export_evidence(run_directory(state), root / 'evidence.tar.gz')
            self.assertEqual(0, result, transcript.getvalue() + f"\nResources: {root}")
            run = run_directory(state)
            validation = read_json(artifact_path(run, STAGES[4], 'repositories', 'service'))
            self.assertEqual('validated', validation['status'])
            self.assertEqual([str(target)], validation['inventory']['java_versions'])
            self.assertTrue(all(proof['status'] == 'executed' for proof in validation['test_evidence']))
            self.assertGreater(sum(proof['counts']['tests'] - proof['counts']['skipped'] for proof in validation['test_evidence']), 0)
            self.assertTrue(validation['inventory']['dependencies'])
            local = run / 'local-repositories/service'
            self.assertNotEqual(original, git(local, 'rev-parse', 'HEAD'))
            git(local, 'merge-base', '--is-ancestor', original, 'HEAD')
            self.assertEqual(original, git(source, 'rev-parse', 'HEAD'))

    def test_real_migration_matrix(self):
        chosen = os.environ.get('JAVA_UPDATE_CASE')
        cases = [chosen] if chosen else list(CASES)
        self.assertTrue(set(cases).issubset(CASES), f'unknown matrix case: {chosen}')
        for case in cases:
            with self.subTest(case=case):
                self.migrate(case)
