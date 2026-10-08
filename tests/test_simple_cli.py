"""The single-repository CLI preserves the portfolio workflow's publishing gates."""
import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from java_update_tool.core import PortfolioError, STAGES, artifact_path, read_json
from java_update_tool.runs import git, command
import test_workflow as fixtures

spec = importlib.util.spec_from_file_location('single_repo_cli', Path(__file__).resolve().parents[1] / 'simple-cli-apps/modernize.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class SimpleCliTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkflowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.destination = self.root / 'destination.git'
        command(['git', 'init', '--bare', str(self.destination)], self.root)
        self.args = ['--source', 'source', '--destination', 'destination.git', '--branch', 'modernized/java-21',
                     '--java', '21', '--config', 'config.yml', '--state', 'state']
        self.output = io.StringIO()

    def invoke(self, *extra, args=None):
        with contextlib.chdir(self.root), contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
            return cli.main([*(self.args if args is None else args), *extra])

    def test_preview_from_another_directory_resolves_inputs_and_preserves_source_ref(self):
        rewrite = self.fixture.config['migration']['openrewrite']
        rewrite.update(source_lock='pinned.lock.json', source_cache='private-cache')
        self.fixture.save()
        (self.root / 'pinned.lock.json').write_text('{}')
        self.assertEqual(0, self.invoke('--source-ref', 'v1'), self.output.getvalue())
        run = self.fixture.run_root()
        receipt = read_json(run / 'run.json')
        self.assertEqual(git(self.fixture.source, 'rev-parse', 'v1'), receipt['sources']['app']['commit'])
        self.assertEqual(str(self.root / 'pinned.lock.json'), receipt['config']['migration']['openrewrite']['source_lock'])
        self.assertEqual(str(self.root / 'private-cache'), receipt['config']['migration']['openrewrite']['source_cache'])
        self.assertEqual(str(self.destination), receipt['workflow']['publishing']['repositories']['app']['url'])
        self.assertEqual('modernized/java-21', receipt['workflow']['publishing']['default_branch'])
        self.assertEqual(3, len(receipt['stages']))
        self.assertFalse((run / STAGES[3]).exists())
        self.assertEqual('', git(self.destination, 'for-each-ref'))
        self.assertEqual(1, len(list((self.root / 'state/inputs').glob('*/repositories.json'))))

    def test_validated_commit_publishes_to_new_branch_in_populated_destination(self):
        unrelated = self.root / 'unrelated'
        unrelated.mkdir()
        git(unrelated, 'init', '-b', 'main')
        (unrelated / 'README.md').write_text('Existing unrelated destination history\n')
        git(unrelated, 'add', '--all')
        git(unrelated, 'commit', '-m', 'Existing destination')
        original = git(unrelated, 'rev-parse', 'HEAD')
        git(unrelated, 'push', str(self.destination), 'main')
        git(self.destination, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        self.args[self.args.index('--source') + 1] = self.fixture.source.as_uri()
        with self.fixture.external_tools():
            self.assertEqual(0, self.invoke('--execute'), self.output.getvalue())
        run = self.fixture.run_root()
        validation = read_json(artifact_path(run, STAGES[4], 'repositories', 'app'))
        publishing = read_json(artifact_path(run, STAGES[5], 'repositories', 'app'))
        self.assertEqual('published', publishing['status'])
        self.assertEqual(validation['commit'], git(self.destination, 'rev-parse', 'modernized/java-21'))
        self.assertEqual(original, git(self.destination, 'rev-parse', 'main'))
        self.assertEqual('refs/heads/main', git(self.destination, 'symbolic-ref', 'HEAD'))
        self.assertEqual(self.fixture.original, git(self.fixture.source, 'rev-parse', 'HEAD'))
        with self.fixture.external_tools():
            self.assertEqual(0, self.invoke('--execute', '--resume', run.name), self.output.getvalue())
        self.assertEqual(run, self.fixture.run_root())
        changed = self.args.copy()
        changed[changed.index('--branch') + 1] = 'another-branch'
        self.assertEqual(2, self.invoke('--execute', '--resume', run.name, args=changed))
        self.assertIn('source policy or CLI overrides changed', self.output.getvalue())
        self.assertEqual('published', read_json(run / 'run.json')['status'])

    def test_divergent_destination_branch_is_preserved(self):
        unrelated = self.root / 'unrelated'
        unrelated.mkdir()
        git(unrelated, 'init', '-b', 'modernized/java-21')
        (unrelated / 'README.md').write_text('Destination branch has unrelated changes\n')
        git(unrelated, 'add', '--all')
        git(unrelated, 'commit', '-m', 'Existing destination branch')
        original = git(unrelated, 'rev-parse', 'HEAD')
        git(unrelated, 'push', str(self.destination), 'modernized/java-21')
        with self.fixture.external_tools():
            self.assertEqual(1, self.invoke('--execute'), self.output.getvalue())
        result = read_json(artifact_path(self.fixture.run_root(), STAGES[5], 'repositories', 'app'))
        self.assertEqual('failed', result['targets']['dst_repo']['status'])
        self.assertEqual(original, git(self.destination, 'rev-parse', 'modernized/java-21'))

    def test_failed_tests_block_local_and_destination_publication(self):
        def failing_build(argv, cwd, timeout=300):
            if 'test' in argv:
                raise PortfolioError('app tests failed')
            return self.fixture.fake_build(argv, cwd, timeout)
        with patch('java_update_tool.cli.execute_migration', side_effect=self.fixture.fake_migration), \
             patch('java_update_tool.validation.command', side_effect=failing_build):
            self.assertEqual(1, self.invoke('--execute'), self.output.getvalue())
        run = self.fixture.run_root()
        self.assertFalse((run / STAGES[5]).exists())
        self.assertFalse((run / 'local-repositories').exists())
        self.assertEqual('', git(self.destination, 'for-each-ref'))

    def test_local_only_execution_never_pushes_to_destination(self):
        with self.fixture.external_tools():
            self.assertEqual(0, self.invoke('--execute', '--local-only'), self.output.getvalue())
        run = self.fixture.run_root()
        result = read_json(artifact_path(run, STAGES[5], 'repositories', 'app'))
        self.assertEqual({'local_repo'}, set(result['targets']))
        self.assertTrue((run / 'local-repositories/app').is_dir())
        self.assertEqual('', git(self.destination, 'for-each-ref'))

    def test_invalid_branch_or_embedded_credentials_fail_before_retaining_inputs(self):
        for flag, value in (('--branch', 'bad branch'), ('--source', 'https://user:secret@example.com/app.git'),
                            ('--destination', 'https://user:secret@example.com/destination.git')):
            args = self.args.copy()
            args[args.index(flag) + 1] = value
            with self.subTest(flag=flag):
                self.assertEqual(2, self.invoke(args=args))
                self.assertFalse((self.root / 'state').exists())
