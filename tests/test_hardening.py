import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import tarfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import java_migrator as engine
from java_update_tool.core import PortfolioError, STAGES, artifact_path, read_json, write_json
from java_update_tool.operations import session, destination_identity
from java_update_tool.runs import command, git
import test_workflow as fixtures


class HardeningTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    save = fixtures.WorkflowTests.save
    invoke = fixtures.WorkflowTests.invoke
    run_root = fixtures.WorkflowTests.run_root
    fake_migration = fixtures.WorkflowTests.fake_migration
    fake_build = fixtures.WorkflowTests.fake_build
    external_tools = fixtures.WorkflowTests.external_tools
    add_worker = fixtures.WorkflowTests.add_worker

    def pipeline(self, through=STAGES[5]):
        with self.external_tools():
            code, output = self.invoke('run', '--through', through, '--execute')
        self.assertEqual(0, code, output)
        return self.run_root()

    def test_explicit_migration_restart_creates_fresh_attempts(self):
        root = self.pipeline(STAGES[4])
        path = artifact_path(root, STAGES[3], 'repositories', 'api')
        first = read_json(path)
        for stage in (STAGES[3], STAGES[2]):
            with self.external_tools():
                code, output = self.invoke('run', '--resume', root.name, '--from', stage,
                                           '--through', STAGES[3], '--execute')
            self.assertEqual(0, code, output)
            second = read_json(path)
            self.assertNotEqual(first['output'], second['output'])
            self.assertTrue(Path(first['output']).is_dir())
            self.assertNotIn(STAGES[4], read_json(root / 'run.json')['stages'])
            first = second

    def test_plain_resume_reuses_successful_migrations_after_partial_failure(self):
        self.add_worker(independent=True)
        def migrate(argv):
            if Path(argv[2]).name == 'worker':
                raise PortfolioError('temporary migration failure')
            return self.fake_migration(argv)
        with patch('java_update_tool.cli.execute_migration', side_effect=migrate):
            code, output = self.invoke('run', '--through', STAGES[3], '--execute')
        self.assertEqual(1, code, output)
        root = self.run_root()
        path = artifact_path(root, STAGES[3], 'repositories', 'api')
        first = read_json(path)
        with patch('java_update_tool.cli.execute_migration', side_effect=self.fake_migration) as execute:
            code, output = self.invoke('run', '--resume', root.name, '--through', STAGES[3], '--execute')
        self.assertEqual(0, code, output)
        self.assertEqual(1, execute.call_count)
        self.assertEqual(first, read_json(path))

    def test_restarted_migration_requires_fresh_validation_before_publication(self):
        root = self.pipeline(STAGES[4])
        with self.external_tools():
            code, output = self.invoke('run', '--resume', root.name, '--from', STAGES[3],
                                       '--through', STAGES[3], '--execute')
        self.assertEqual(0, code, output)
        code, output = self.invoke('run', '--resume', root.name, '--from', STAGES[5],
                                   '--through', STAGES[5], '--execute')
        self.assertEqual(2, code, output)
        self.assertIn('requires current completed validation', output)
        self.assertFalse((root / 'local-repositories').exists())

    def test_migration_uses_configured_discovery_depth(self):
        self.config['discovery'] = {'max_depth': 6}
        self.save()
        nested = self.source / 'a/b/c/d/e/f'
        nested.mkdir(parents=True)
        (nested / 'pom.xml').write_text(fixtures.POM)
        git(self.source, 'add', '--all')
        git(self.source, 'commit', '-m', 'Add deeply nested build')
        code, output = self.invoke('run', '--through', STAGES[3])
        self.assertEqual(0, code, output)
        root = self.run_root()
        discovered = read_json(artifact_path(root, STAGES[0], 'repositories', 'api'))
        migration = read_json(artifact_path(root, STAGES[3], 'repositories', 'api'))
        args = engine.parse_args(migration['command'][2:])
        builds = engine.discover_builds(Path(args.sources[0]), args.build_tool, args.max_depth)
        self.assertEqual(6, args.max_depth)
        self.assertEqual({item['path'] for item in discovered['projects']},
                         {str(build.path.relative_to(Path(args.sources[0]))) for build in builds})

    def test_invalid_discovery_options_fail_before_creating_a_run(self):
        for options in ({'max_depth': -1}, {'max_depth': True}, {'max_depth': '5'},
                        {'build_tool': 'unknown'}, {'build_tool': []}, []):
            with self.subTest(options=options):
                self.config['discovery'] = options
                self.save()
                code, output = self.invoke('validate')
                self.assertEqual(2, code, output)
                self.assertIn('discovery', output)
        self.assertFalse(self.state.exists())

    def test_empty_resume_preserves_failed_and_published_outcomes(self):
        with self.external_tools(), patch('java_update_tool.validation.command', side_effect=PortfolioError('build failed')):
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(1, code, output)
        root = self.run_root()
        before = read_json(root / 'run.json')
        self.assertEqual(1, self.invoke('run', '--resume', root.name)[0])
        self.assertEqual(before, read_json(root / 'run.json'))
        with self.external_tools():
            code, output = self.invoke('run', '--resume', root.name, '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, output)
        before = read_json(root / 'run.json')
        self.assertEqual(0, self.invoke('run', '--resume', root.name)[0])
        self.assertEqual(before, read_json(root / 'run.json'))

    def test_missing_migrated_build_retains_validation_failure(self):
        root = self.pipeline(STAGES[3])
        result = read_json(artifact_path(root, STAGES[3], 'repositories', 'api'))
        output = Path(result['output'])
        git(output, 'rm', 'pom.xml')
        git(output, 'commit', '-m', 'Remove build')
        code, transcript = self.invoke('run', '--resume', root.name, '--through', STAGES[5], '--execute')
        self.assertEqual(1, code, transcript)
        validation = read_json(artifact_path(root, STAGES[4], 'repositories', 'api'))
        self.assertEqual('failed', validation['status'])
        self.assertIn('no Maven or Gradle build', validation['error'])
        self.assertFalse((root / STAGES[5]).exists())

    def test_custom_integration_suite_preserves_builtin_evidence(self):
        from java_update_tool.validation import verify_validation_evidence
        pom = self.source / 'pom.xml'
        pom.write_text(pom.read_text().replace('</project>', '<build><plugins><plugin><artifactId>maven-failsafe-plugin</artifactId></plugin></plugins></build></project>'))
        git(self.source, 'add', 'pom.xml')
        git(self.source, 'commit', '-m', 'Declare integration suite')
        self.config['workflow']['validation'] = {'suites': [
            {'name': 'integration', 'command': ['custom-integration'], 'reports': ['target/custom/TEST-*.xml']}]}
        self.save()
        def build(argv, cwd, timeout=300):
            result = self.fake_build(argv, cwd, timeout)
            if 'verify' in argv or 'custom-integration' in argv:
                custom = 'custom-integration' in argv
                path = cwd / ('target/custom/TEST-contract.xml' if custom else 'target/failsafe-reports/TEST-contract.xml')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f'<testsuite tests="{3 if custom else 2}"/>')
            return result
        with self.external_tools(), patch('java_update_tool.validation.command', side_effect=build):
            code, transcript = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, transcript)
        value = read_json(artifact_path(self.run_root(), STAGES[4], 'repositories', 'api'))
        verify_validation_evidence(value)
        proofs = [proof for proof in value['test_evidence'] if proof['suite'] == 'integration']
        self.assertEqual([2, 3], [proof['counts']['tests'] for proof in proofs])
        self.assertNotEqual(proofs[0]['reports'][0]['resource'], proofs[1]['reports'][0]['resource'])

    def test_failed_custom_suite_preserves_completed_build_evidence(self):
        self.config['workflow']['validation'] = {'suites': [
            {'name': 'contract', 'command': ['contract-check'], 'reports': ['target/contracts/TEST-*.xml']}]}
        self.save()
        def build(argv, cwd, timeout=300):
            if 'contract-check' in argv:
                raise PortfolioError('contract failed')
            return self.fake_build(argv, cwd, timeout)
        with self.external_tools(), patch('java_update_tool.validation.command', side_effect=build):
            code, transcript = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(1, code, transcript)
        root = self.run_root()
        value = read_json(artifact_path(root, STAGES[4], 'repositories', 'api'))
        self.assertEqual('contract failed', value['error'])
        self.assertEqual(['passed', 'failed'], [check['status'] for check in value['checks']])
        self.assertEqual('executed', value['test_evidence'][0]['status'])
        self.assertTrue(Path(value['test_evidence'][0]['reports'][0]['resource']).is_file())
        self.assertFalse((root / 'local-repositories').exists())

    def test_missing_engine_summary_records_executed_migration_failure(self):
        self.add_worker()
        def migrate(argv):
            completed = self.fake_migration(argv)
            workspace = Path(argv[argv.index('--workspace') + 1])
            (workspace / 'reports/summary.json').unlink()
            return completed
        with patch('java_update_tool.cli.execute_migration', side_effect=migrate) as execute:
            code, transcript = self.invoke('run', '--through', STAGES[3], '--execute')
        self.assertEqual(1, code, transcript)
        self.assertEqual(1, execute.call_count)
        root = self.run_root()
        failed = read_json(artifact_path(root, STAGES[3], 'repositories', 'api'))
        blocked = read_json(artifact_path(root, STAGES[3], 'repositories', 'worker'))
        self.assertEqual('failed', failed['status'])
        self.assertTrue(failed['executed'])
        self.assertEqual(0, failed['exit_code'])
        self.assertIn('missing', failed['error'])
        self.assertEqual(['api'], blocked['blocked_by'])
        self.assertFalse(blocked['executed'])

    def test_resume_partial_snapshot_keeps_first_and_finishes_second(self):
        second = self.root / 'second'
        command(['git', 'clone', str(self.source), str(second)], self.root)
        (second / 'dirty.txt').write_text('pending')
        self.portfolio['repositories'].append({**self.portfolio['repositories'][0], 'repo_name': 'worker', 'source': str(second)})
        self.save()
        code, _ = self.invoke('run', '--through', STAGES[0])
        self.assertEqual(2, code)
        root = self.run_root()
        self.assertEqual(['api'], list(read_json(root / 'run.json')['sources']))
        first = read_json(root / 'run.json')['sources']['api']
        (second / 'dirty.txt').unlink()
        code, output = self.invoke('run', '--resume', root.name, '--through', STAGES[0])
        self.assertEqual(0, code, output)
        self.assertEqual(first, read_json(root / 'run.json')['sources']['api'])
        self.assertIn('worker', read_json(root / 'run.json')['sources'])

    def test_resume_recovers_promoted_snapshot_without_run_receipt(self):
        from java_update_tool import runs
        original = runs.write_json
        failed = False
        def interrupted(path, value):
            nonlocal failed
            if path.name == 'run.json' and value.get('sources') and not failed:
                failed = True
                raise OSError('interrupted after promotion')
            return original(path, value)
        with patch('java_update_tool.runs.write_json', side_effect=interrupted):
            self.assertEqual(2, self.invoke('run', '--through', STAGES[0])[0])
        root = self.run_root()
        self.assertTrue((root / 'sources/api/.git/java-update-snapshot.json').is_file())
        self.assertEqual(0, self.invoke('run', '--resume', root.name, '--through', STAGES[0])[0])

    def test_rejected_resume_and_approval_preserve_published_outcome(self):
        root = self.pipeline()
        before = read_json(root / 'run.json')
        self.config['workflow']['show_diffs'] = True
        self.save()
        self.assertEqual(2, self.invoke('run', '--resume', root.name, '--through', STAGES[5], '--execute')[0])
        self.assertEqual(2, self.invoke('approve', root.name, '--stage', STAGES[4])[0])
        self.assertEqual(before, read_json(root / 'run.json'))
        events = [json.loads(line) for line in (root / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(2, sum(item['event'] == 'invocation-rejected' for item in events))

    def test_new_validated_commit_updates_prior_local_receipt(self):
        root = self.pipeline()
        validation = read_json(artifact_path(root, STAGES[4], 'repositories', 'api'))
        output = Path(validation['output'])
        (output / 'notes.txt').write_text('Additional approved change')
        git(output, 'add', 'notes.txt')
        git(output, 'commit', '-m', 'Follow-up change')
        expected = git(output, 'rev-parse', 'HEAD')
        with self.external_tools():
            code, transcript = self.invoke('run', '--resume', root.name, '--from', STAGES[4], '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, transcript)
        self.assertEqual(expected, git(root / 'local-repositories/api', 'rev-parse', 'HEAD'))
        receipt = read_json(artifact_path(root, STAGES[5], 'repositories', 'api'))
        self.assertEqual(expected, receipt['targets']['local_repo']['commit'])
        self.assertEqual(validation['commit'], receipt['history'][0]['receipt']['commit'])

    def test_compiler_execution_overrides_and_incompatible_test_compilation(self):
        for test_level, expected in ((21, 0), (17, 1)):
            with self.subTest(test_level=test_level):
                def build(argv, cwd, timeout=300):
                    result = self.fake_build(argv, cwd, timeout)
                    if 'help:effective-pom' in argv:
                        path = Path(next(arg.split('=', 1)[1] for arg in argv if arg.startswith('-Doutput=')))
                        plugin = '<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><configuration><release>17</release></configuration><executions>'
                        for name, goal, level in (('default-compile', 'compile', 21), ('default-testCompile', 'testCompile', test_level)):
                            plugin += f'<execution><id>{name}</id><goals><goal>{goal}</goal></goals><configuration><release>{level}</release></configuration></execution>'
                        path.write_text(path.read_text().replace('</project>', plugin + '</executions></plugin></plugins></build></project>'))
                    return result
                with patch('java_update_tool.cli.execute_migration', side_effect=self.fake_migration), patch('java_update_tool.validation.command', side_effect=build):
                    code, transcript = self.invoke('run', '--through', STAGES[5], '--execute')
                self.assertEqual(expected, code, transcript)

    def test_declared_integration_requires_its_own_fresh_reports(self):
        pom = self.source / 'pom.xml'
        pom.write_text(pom.read_text().replace('</project>', '<build><plugins><plugin><artifactId>maven-failsafe-plugin</artifactId></plugin></plugins></build></project>'))
        git(self.source, 'add', 'pom.xml')
        git(self.source, 'commit', '-m', 'Declare integration checks')
        for emit, expected in ((False, 1), (True, 0)):
            def build(argv, cwd, timeout=300):
                result = self.fake_build(argv, cwd, timeout)
                if emit and 'verify' in argv:
                    report = cwd / 'target/failsafe-reports/TEST-contract.xml'
                    report.parent.mkdir(parents=True, exist_ok=True)
                    report.write_text('<testsuite tests="2"/>')
                return result
            with self.external_tools(), patch('java_update_tool.validation.command', side_effect=build):
                code, transcript = self.invoke('run', '--through', STAGES[5], '--execute')
            self.assertEqual(expected, code, transcript)
        value = read_json(artifact_path(self.run_root(), STAGES[4], 'repositories', 'api'))
        self.assertEqual({'unit', 'integration'}, {item['suite'] for item in value['test_evidence']})

    def test_legacy_dependency_evidence_requires_fresh_validation(self):
        root = self.pipeline(STAGES[4])
        evidence = artifact_path(root, STAGES[4], 'repositories', 'api')
        value = read_json(evidence)
        value.pop('evidence_version')
        write_json(evidence, value)
        self.add_worker(independent=True)
        self.config['workflow']['dependency_evidence'] = {'api': str(evidence)}
        self.save()
        code, transcript = self.invoke('run', '--repo', 'worker', '--through', STAGES[3], '--execute')
        self.assertEqual(2, code, transcript)
        self.assertIn('needs validated compatibility evidence', transcript)

    def test_suite_name_cannot_escape_evidence_directory(self):
        self.config['workflow']['validation'] = {'suites': [{'name': '../../escape', 'command': ['true'], 'reports': ['target/TEST-*.xml']}]}
        self.save()
        code, transcript = self.invoke('validate')
        self.assertEqual(2, code, transcript)
        self.assertIn('custom suites', transcript)

    def test_host_scoped_source_and_destination_credentials(self):
        from java_update_tool.publishing import remote_auth
        from java_update_tool.runs import source_auth
        workflow = {'source_credentials': {'git.internal': 'SOURCE_TOKEN'}}
        options = {'provider': 'gitlab', 'api_url': 'https://gitlab.com/api/v4', 'token_env': 'DEST_TOKEN'}
        observed = []
        def execute(argv, cwd, env, timeout, **kwargs):
            observed.append(env.get('JAVA_UPDATE_GIT_TOKEN'))
            return ''
        with patch.dict(os.environ, {'GH_TOKEN': 'github-source-secret', 'SOURCE_TOKEN': 'internal-source-secret', 'DEST_TOKEN': 'gitlab-destination-secret'}), patch('java_update_tool.runs.execute', side_effect=execute):
            for url, credentials in (
                ('https://github.com/acme/source.git', source_auth('https://github.com/acme/source.git', workflow)),
                ('https://git.internal/acme/source.git', source_auth('https://git.internal/acme/source.git', workflow)),
                ('https://github.com/acme/source.git', remote_auth('https://github.com/acme/source.git', options, workflow, 'src_repo')),
                ('https://gitlab.com/acme/new.git', remote_auth('https://gitlab.com/acme/new.git', options, workflow, 'dst_repo')),
                ('https://unrelated.example/acme/new.git', remote_auth('https://unrelated.example/acme/new.git', options, workflow, 'dst_repo')),
            ):
                with credentials:
                    command(['git', 'ls-remote', url], self.root)
        self.assertEqual(['github-source-secret', 'internal-source-secret', 'github-source-secret', 'gitlab-destination-secret', None], observed)

    def test_successful_verbose_check_keeps_outcome_with_bounded_logs(self):
        root = self.pipeline(STAGES[2])
        options = read_json(root / 'run.json')['workflow']
        options['diagnostics'].update(check_bytes=128, run_bytes=256)
        with session(root, options):
            command([sys.executable, '-c', "print('x'*2000)"], self.root)
        metadata = [read_json(path) for path in (root / 'diagnostics').glob('*.json')]
        check = next(item for item in metadata if item['command'][0] == sys.executable)
        self.assertEqual(0, check['exit_code'])
        self.assertTrue(check['truncated'])
        self.assertLessEqual(Path(check['log']).stat().st_size, 128)

    def test_output_timeout_cleans_up_descendants_after_parent_exit(self):
        root = self.pipeline(STAGES[2])
        started = time.monotonic()
        script = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); print('parent exited')"
        with session(root, read_json(root / 'run.json')['workflow']):
            with self.assertRaisesRegex(PortfolioError, 'timed out'):
                command([sys.executable, '-c', script], self.root, timeout=1)
        self.assertLess(time.monotonic() - started, 4)

    def test_engine_post_check_redacts_and_records_failure_diagnostics(self):
        root = self.pipeline(STAGES[2])
        env = os.environ.copy()
        env.update(JAVA_UPDATE_RUN_ROOT=str(root), CUSTOM_SECRET_TOKEN='private-post-check-value')
        args = engine.parse_args(['example'])
        check = engine.diagnostic_command('custom', [sys.executable, '-c', "import sys; print('private-post-check-value',file=sys.stderr); sys.exit(1)"], engine.BuildRoot(self.source, 'maven'), env, args)
        self.assertEqual('warning', check.status)
        self.assertEqual(1, check.returncode)
        self.assertIn('Diagnostics:', check.output)
        self.assertNotIn('private-post-check-value', check.output)
        self.assertNotIn('private-post-check-value', ' '.join(check.command))
        self.assertNotIn('private-post-check-value', ''.join(path.read_text() for path in (root / 'diagnostics').glob('*.log')))

    def test_shared_destination_lock_blocks_push_and_independent_run_proceeds(self):
        self.config['workflow']['publishing'] = {'enabled': True, 'targets': ['src_repo']}
        self.save()
        root = self.pipeline(STAGES[4])
        identity = 'destination:' + destination_identity(self.source.as_uri())
        script = "from pathlib import Path; import time; from java_update_tool.operations import lock; from contextlib import ExitStack; s=ExitStack(); s.enter_context(lock(Path(%r),%r,'shared destination writer')); print('locked',flush=True); time.sleep(30)" % (str(self.state / '.locks'), identity)
        child = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertEqual('locked', child.stdout.readline().strip())
        with self.external_tools():
            code, transcript = self.invoke('run', '--resume', root.name, '--through', STAGES[5], '--execute')
        self.assertEqual(1, code, transcript)
        value = read_json(artifact_path(root, STAGES[5], 'repositories', 'api'))
        self.assertIn('shared destination writer', value['targets']['src_repo']['error'])
        self.config['workflow']['publishing'] = {'targets': ['local_repo']}
        self.save()
        self.pipeline()
        child.terminate()
        child.wait(timeout=5)
        child.stdout.close()
        self.config['workflow']['publishing'] = {'enabled': True, 'targets': ['src_repo']}
        self.save()
        with self.external_tools():
            code, transcript = self.invoke('run', '--resume', root.name, '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, transcript)

    def test_local_human_change_blocks_receipt_reconciliation(self):
        root = self.pipeline()
        (root / 'local-repositories/api/notes.txt').write_text('Human edit')
        with self.external_tools():
            code, _ = self.invoke('run', '--resume', root.name, '--from', STAGES[5], '--through', STAGES[5], '--execute')
        self.assertEqual(1, code)
        self.assertEqual('Human edit', (root / 'local-repositories/api/notes.txt').read_text())

    def test_revalidated_source_publication_updates_owned_branch_in_all_history_scope(self):
        self.config['workflow']['publishing'] = {'enabled': True, 'targets': ['src_repo'], 'history': 'all'}
        self.save()
        root = self.pipeline()
        validation = read_json(artifact_path(root, STAGES[4], 'repositories', 'api'))
        output = Path(validation['output'])
        (output / 'notes.txt').write_text('Reviewed follow-up')
        git(output, 'add', 'notes.txt')
        git(output, 'commit', '-m', 'Reviewed follow-up')
        expected = git(output, 'rev-parse', 'HEAD')
        with self.external_tools():
            code, transcript = self.invoke('run', '--resume', root.name, '--from', STAGES[4], '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, transcript)
        branch = read_json(root / 'run.json')['sources']['api']['migration_branch']
        self.assertEqual(expected, git(self.source, 'rev-parse', branch))
        self.assertEqual(self.original, git(self.source, 'rev-parse', 'master'))

    def test_source_branches_unique_across_runs_and_reused_on_retry(self):
        self.config['workflow']['publishing'] = {'enabled': True, 'targets': ['src_repo'], 'source_branch': 'automation/java-{java}'}
        self.save()
        first = self.pipeline()
        one = read_json(first / 'run.json')['sources']['api']['migration_branch']
        (self.source / 'notes.txt').write_text('Upstream advances')
        git(self.source, 'add', '--all')
        git(self.source, 'commit', '-m', 'Advance source')
        second = self.pipeline()
        two = read_json(second / 'run.json')['sources']['api']['migration_branch']
        self.assertNotEqual(one, two)
        before = git(self.source, 'rev-parse', two)
        with self.external_tools():
            code, transcript = self.invoke('run', '--resume', second.name, '--from', STAGES[5], '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, transcript)
        self.assertEqual(before, git(self.source, 'rev-parse', two))

    def test_local_default_branch_and_explicit_current_selection(self):
        git(self.source, 'checkout', '-b', 'feature')
        (self.source / 'notes.txt').write_text('Feature content')
        git(self.source, 'add', '--all')
        git(self.source, 'commit', '-m', 'Feature')
        self.assertEqual(0, self.invoke('run', '--through', STAGES[0])[0])
        receipt = read_json(self.run_root() / 'run.json')['sources']['api']
        self.assertEqual(self.original, receipt['commit'])
        self.assertEqual('master', receipt['default_branch'])
        self.assertEqual(0, self.invoke('run', '--source-selection', 'current', '--through', STAGES[0])[0])
        self.assertEqual(git(self.source, 'rev-parse', 'HEAD'), read_json(self.run_root() / 'run.json')['sources']['api']['commit'])

    def test_all_history_drift_never_publishes_changed_refs(self):
        self.config['workflow']['publishing'] = {'history': 'all'}
        self.save()
        root = self.pipeline(STAGES[4])
        git(self.source, 'branch', 'new-unreviewed-branch')
        with self.external_tools():
            code, _ = self.invoke('run', '--resume', root.name, '--through', STAGES[5], '--execute')
        self.assertEqual(1, code)
        self.assertFalse((root / 'local-repositories/api').exists())
        result = read_json(artifact_path(root, STAGES[5], 'repositories', 'api'))
        self.assertIn('captured source refs changed', result['error'])

    def test_stale_reports_do_not_qualify_and_exemption_is_explicit(self):
        root = self.pipeline(STAGES[3])
        output = Path(read_json(artifact_path(root, STAGES[3], 'repositories', 'api'))['output'])
        report = output / 'target/surefire-reports/TEST-stale.xml'
        report.parent.mkdir(parents=True)
        report.write_text('<testsuite tests="12"/>')
        def no_tests(argv, cwd, timeout=300):
            if 'test' in argv:
                return ''
            return self.fake_build(argv, cwd, timeout)
        with patch('java_update_tool.validation.command', side_effect=no_tests):
            code, _ = self.invoke('run', '--resume', root.name, '--through', STAGES[5], '--execute')
        self.assertEqual(1, code)
        value = read_json(artifact_path(root, STAGES[4], 'repositories', 'api'))
        self.assertIn('fresh executed tests', value['error'])
        self.config['workflow']['validation'] = {'test_exemptions': {'unit': 'This generated BOM contains no executable code'}}
        self.save()
        with patch('java_update_tool.cli.execute_migration', side_effect=self.fake_migration), patch('java_update_tool.validation.command', side_effect=no_tests):
            self.assertEqual(0, self.invoke('run', '--through', STAGES[5], '--execute')[0])
        value = read_json(artifact_path(self.run_root(), STAGES[4], 'repositories', 'api'))
        self.assertEqual('exempted', value['test_evidence'][0]['status'])
        self.assertIn('generated BOM', value['test_evidence'][0]['reason'])

    def test_retained_report_tampering_blocks_approval_or_publication(self):
        for mode, expected in (('unattended', 1), ('manual', 2)):
            with self.subTest(mode=mode):
                self.config['workflow'].update(mode=mode, checkpoints=[STAGES[4]])
                self.save()
                with self.external_tools():
                    code, transcript = self.invoke('run', '--through', STAGES[4], '--execute')
                self.assertEqual(3 if mode == 'manual' else 0, code, transcript)
                root = self.run_root()
                if mode == 'manual':
                    self.assertEqual(0, self.invoke('approve', root.name, '--stage', STAGES[4])[0])
                value = read_json(artifact_path(root, STAGES[4], 'repositories', 'api'))
                Path(value['test_evidence'][0]['reports'][0]['resource']).write_text('<testsuite tests="999"/>')
                with self.external_tools():
                    code, transcript = self.invoke('run', '--resume', root.name, '--through', STAGES[5], '--execute')
                self.assertEqual(expected, code, transcript)
                self.assertFalse((root / 'local-repositories/api').exists())

    def test_skipped_and_failed_reports_block_publication(self):
        for counts in ({'tests': 2, 'skipped': 2}, {'tests': 2, 'failures': 1}):
            with self.subTest(counts=counts):
                def build(argv, cwd, timeout=300):
                    result = self.fake_build(argv, cwd, timeout)
                    if 'test' in argv:
                        (cwd / 'target/surefire-reports/TEST-regression.xml').write_text('<testsuite ' + ' '.join(f'{key}="{value}"' for key, value in counts.items()) + '/>')
                    return result
                with patch('java_update_tool.cli.execute_migration', side_effect=self.fake_migration), patch('java_update_tool.validation.command', side_effect=build):
                    self.assertEqual(1, self.invoke('run', '--through', STAGES[5], '--execute')[0])
                self.assertFalse((self.run_root() / 'local-repositories/api').exists())

    def test_effective_compiler_release_overrides_stale_java_metadata(self):
        def build(argv, cwd, timeout=300):
            result = self.fake_build(argv, cwd, timeout)
            if 'help:effective-pom' in argv:
                path = Path(next(arg.split('=', 1)[1] for arg in argv if arg.startswith('-Doutput=')))
                path.write_text(path.read_text().replace('<properties>', '<properties><java.version>17</java.version>'))
            return result
        with patch('java_update_tool.cli.execute_migration', side_effect=self.fake_migration), patch('java_update_tool.validation.command', side_effect=build):
            code, transcript = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, transcript)
        value = read_json(artifact_path(self.run_root(), STAGES[4], 'repositories', 'api'))
        self.assertEqual('17', value['inventory']['compiler_metadata'][0]['java.version'])

    def test_independent_nested_build_required_and_reasoned_exclusion(self):
        nested = self.source / 'independent'
        nested.mkdir()
        (nested / 'pom.xml').write_text((self.source / 'pom.xml').read_text())
        git(self.source, 'add', '--all')
        git(self.source, 'commit', '-m', 'Add independent build')
        self.assertEqual(2, len(engine.discover_builds(self.source, 'auto', 5)))
        def build(argv, cwd, timeout=300):
            if cwd.name == 'independent':
                raise PortfolioError('nested build failed')
            return self.fake_build(argv, cwd, timeout)
        with patch('java_update_tool.cli.execute_migration', side_effect=self.fake_migration), patch('java_update_tool.validation.command', side_effect=build):
            self.assertEqual(1, self.invoke('run', '--through', STAGES[5], '--execute')[0])
        self.config['workflow']['validation'] = {'build_roots': ['.'], 'exclusions': {'independent': 'Separate legacy application, intentionally excluded'}}
        self.save()
        with patch('java_update_tool.cli.execute_migration', side_effect=self.fake_migration), patch('java_update_tool.validation.command', side_effect=build):
            code, transcript = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, transcript)
        value = read_json(artifact_path(self.run_root(), STAGES[4], 'repositories', 'api'))
        self.assertEqual('selected-builds', value['scope']['coverage'])
        self.assertIn('independent', value['scope']['excluded'])

    def test_diagnostics_redaction_limits_and_evidence_export(self):
        root = self.pipeline()
        options = read_json(root / 'run.json')['workflow']
        options['diagnostics']['check_bytes'] = 1024
        with patch.dict(os.environ, {'TEST_SECRET_TOKEN': 'sensitive-token-123'}), session(root, options):
            with self.assertRaises(PortfolioError):
                command([sys.executable, '-c', "import sys; print('sensitive-token-123'); print('x'*2000); sys.exit(1)"], self.root)
        logs = list((root / 'diagnostics').glob('*.log'))
        self.assertTrue(logs)
        self.assertNotIn('sensitive-token-123', ''.join(path.read_text() for path in logs))
        bundle = self.root / 'evidence.tar.gz'
        self.assertEqual(0, self.invoke('export-evidence', root.name, '--output', str(bundle))[0])
        with tarfile.open(bundle) as archive:
            names = archive.getnames()
            self.assertIn('manifest.json', names)
            self.assertIn('05-validation/repositories/api/result.json', names)
            self.assertFalse(any('/.git/' in name for name in names))
        before = read_json(root / 'run.json')
        old = time.time() - 31 * 86400
        for path in logs:
            os.utime(path, (old, old))
        with session(root, options):
            pass
        self.assertFalse(any(path.exists() for path in logs))
        self.assertEqual(before, read_json(root / 'run.json'))

    def test_diagnostic_expiry_and_export_do_not_follow_external_directories(self):
        root = self.pipeline(STAGES[2])
        outside = self.root / 'external-logs'
        outside.mkdir()
        log = outside / 'private.log'
        log.write_text('Unrelated retained log')
        old = time.time() - 31 * 86400
        os.utime(log, (old, old))
        original = root / 'diagnostics'
        original.rename(root / 'original-diagnostics')
        original.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(PortfolioError, 'unsafe diagnostics'):
            with session(root, read_json(root / 'run.json')['workflow']):
                pass
        self.assertTrue(log.exists())
        bundle = self.root / 'safe-evidence.tar.gz'
        self.assertEqual(0, self.invoke('export-evidence', root.name, '--output', str(bundle))[0])
        with tarfile.open(bundle) as archive:
            self.assertNotIn('diagnostics/private.log', archive.getnames())

    def test_prune_dry_run_references_and_durable_local_protection(self):
        first = self.pipeline(STAGES[2])
        receipt = read_json(first / 'run.json')
        receipt['generated_at'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=60)).isoformat()
        write_json(first / 'run.json', receipt)
        second = self.pipeline()
        code, transcript = self.invoke('prune')
        self.assertEqual(0, code, transcript)
        self.assertIn('would-delete', transcript)
        self.assertTrue(first.exists())
        self.config['workflow']['retention'] = {'pins': [first.name]}
        self.save()
        self.assertEqual(0, self.invoke('prune', '--apply')[0])
        self.assertTrue(first.exists())
        self.config['workflow']['retention']['pins'] = []
        self.save()
        self.assertEqual(0, self.invoke('prune', '--apply')[0])
        self.assertFalse(first.exists())
        self.assertTrue((second / 'local-repositories/api').is_dir())

    def test_pruning_protects_dependency_evidence_referenced_by_another_run(self):
        first = self.pipeline(STAGES[4])
        receipt = read_json(first / 'run.json')
        receipt['generated_at'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=60)).isoformat()
        write_json(first / 'run.json', receipt)
        self.config['workflow']['dependency_evidence'] = {'api': str(artifact_path(first, STAGES[4], 'repositories', 'api'))}
        self.save()
        self.pipeline(STAGES[2])
        self.config['workflow']['dependency_evidence'] = {}
        self.save()
        code, transcript = self.invoke('prune', '--apply')
        self.assertEqual(0, code, transcript)
        self.assertIn('referenced by', transcript)
        self.assertTrue(first.exists())

    def test_real_process_lock_blocks_mutation_and_releases_on_exit(self):
        root = self.pipeline(STAGES[2])
        script = "from pathlib import Path; import time; from java_update_tool.operations import lock; from contextlib import ExitStack; s=ExitStack(); s.enter_context(lock(Path(%r),%r,'competing process')); print('locked',flush=True); time.sleep(15)" % (str(self.state / '.locks'), str(root.resolve()))
        child = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertEqual('locked', child.stdout.readline().strip())
        before = read_json(root / 'run.json')
        started = time.monotonic()
        code, transcript = self.invoke('approve', root.name, '--stage', STAGES[2])
        self.assertEqual(2, code)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn('competing process', transcript)
        self.assertEqual(before, read_json(root / 'run.json'))
        child.terminate()
        child.wait(timeout=5)
        child.stdout.close()
        self.assertEqual(0, self.invoke('approve', root.name, '--stage', STAGES[2])[0])
