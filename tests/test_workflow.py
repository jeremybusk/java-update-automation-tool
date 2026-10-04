import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import yaml

from java_update_tool.cli import main
from java_update_tool.core import STAGES, PortfolioError, artifact_path, read_json, write_json
from java_update_tool.runs import git, run_directory, command


POM = '''<project xmlns="http://maven.apache.org/POM/4.0.0">
<modelVersion>4.0.0</modelVersion>
<parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId><version>3.4.2</version></parent>
<groupId>example</groupId><artifactId>app</artifactId><version>1</version>
<properties><java.version>17</java.version></properties>
<dependencyManagement><dependencies><dependency><groupId>org.example</groupId><artifactId>shared</artifactId><version>1.0.0</version></dependency></dependencies></dependencyManagement>
<dependencies><dependency><groupId>org.example</groupId><artifactId>shared</artifactId></dependency></dependencies>
</project>'''


class FakeHost:
    def __init__(self, directory, provider):
        self.directory, self.provider = directory, provider
        self.repos, self.calls = {}, []
        self.authentication_failed = False
        self.review_requests = []
        self.ambiguous_request = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, value):
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())

            def do_GET(self):
                owner.calls.append(('GET', self.path))
                if owner.authentication_failed:
                    return self.respond(401, {})
                if '/pulls' in self.path or '/merge_requests' in self.path:
                    import urllib.parse
                    tail = urllib.parse.urlparse(self.path).path.rsplit('/', 1)[-1]
                    return self.respond(200, owner.review_requests[int(tail) - 1] if tail.isdigit() else owner.review_requests)
                if self.path in {'/user', '/users/acme'}:
                    return self.respond(200, {'login': 'acme', 'type': 'User'})
                if self.path.startswith('/namespaces'):
                    return self.respond(200, [{'id': 12, 'full_path': 'acme'}])
                name = self.path.rsplit('/', 1)[-1].replace('acme%2F', '')
                return self.respond(200 if name in owner.repos else 404, owner.repos.get(name, {}))

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.calls.append(('POST', self.path))
                if self.path.endswith('/pulls') or self.path.endswith('/merge_requests'):
                    branch = data.get('head') or data['source_branch']
                    base = data.get('base') or data['target_branch']
                    source = Path(owner.repos['source']['clone_url'])
                    sha = git(source, 'rev-parse', branch)
                    value = {'number': len(owner.review_requests) + 1, 'iid': len(owner.review_requests) + 1,
                             'state': 'open' if owner.provider == 'github' else 'opened', 'draft': True,
                             'head': {'ref': branch, 'sha': sha}, 'base': {'ref': base}, 'sha': sha,
                             'source_branch': branch, 'target_branch': base, 'body': data.get('body'),
                             'description': data.get('description'), 'html_url': 'https://host/request/1', 'web_url': 'https://host/request/1'}
                    owner.review_requests.append(value)
                    if owner.ambiguous_request:
                        owner.ambiguous_request = False
                        return self.respond(503, {})
                    return self.respond(201, value)
                name = data['name']
                if name in owner.repos:
                    return self.respond(422, {})
                path = owner.directory / name
                path.mkdir()
                git(path, 'init', '--bare')
                value = {'clone_url': str(path), 'http_url_to_repo': str(path), 'id': len(owner.repos) + 1}
                owner.repos[name] = value
                return self.respond(201, value)

            def update(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.calls.append((self.command, self.path, data))
                name = self.path.rsplit('/', 1)[-1].replace('acme%2F', '')
                git(owner.directory / name, 'symbolic-ref', 'HEAD', 'refs/heads/' + data['default_branch'])
                self.respond(200, owner.repos[name])

            do_PATCH = update
            do_PUT = update

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        git(self.source, 'init', '-b', 'master')
        (self.source / 'pom.xml').write_text(POM)
        git(self.source, 'add', '--all')
        git(self.source, 'commit', '-m', 'Initial build')
        git(self.source, 'tag', 'v1')
        (self.source / 'notes.txt').write_text('Original history\n')
        git(self.source, 'add', '--all')
        git(self.source, 'commit', '-m', 'Second commit')
        git(self.source, 'branch', 'maintenance')
        self.original = git(self.source, 'rev-parse', 'HEAD')
        self.config = {
            'schema_version': 1,
            'targets': {'java': {'desired': 21, 'acceptable': [21]}, 'spring_boot': {'desired': '3.5.x', 'acceptable': ['3.5.x']}},
            'alignment': {'dependencies': {'include': ['org.example:*'], 'pins': {'org.example:shared': '2.0.0'}}},
            'migration': {'profile': 'standard', 'openrewrite': {'recipe_repository': 'maven-central',
                'artifacts': ['org.openrewrite.recipe:rewrite-spring:6.40.0']}},
            'workflow': {'mode': 'unattended'},
        }
        self.portfolio = {'schema_version': 1, 'repositories': [
            {'repo_name': 'api', 'source': str(self.source), 'application_id': 'orders', 'application_group_id': 'commerce'}]}
        self.state = self.root / 'state'
        self.calls = []
        self.save()

    def save(self):
        (self.root / 'config.yml').write_text(yaml.safe_dump(self.config))
        (self.root / 'repos.yml').write_text(yaml.safe_dump(self.portfolio))

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = main(['--config', str(self.root / 'config.yml'), '--portfolio', str(self.root / 'repos.yml'),
                         '--state', str(self.state), *args])
        return code, output.getvalue()

    def run_root(self):
        return run_directory(self.state)

    def fake_migration(self, argv):
        source = Path(argv[2])
        output = Path(argv[argv.index('--output') + 1]) / source.name
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, output)
        pom = output / 'pom.xml'
        pom.write_text(pom.read_text().replace('>17<', '>21<').replace('3.4.2', '3.5.1').replace('1.0.0', '2.0.0'))
        git(output, 'add', '--all')
        git(output, 'commit', '-m', 'Migrate validated build')
        workspace = Path(argv[argv.index('--workspace') + 1])
        write_json(workspace / 'reports/summary.json', {'results': [{'status': 'changed'}]})
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def fake_build(self, argv, cwd, timeout=300):
        self.calls.append(argv)
        if 'test' in argv or 'verify' in argv:
            report = cwd / 'target/surefire-reports/TEST-regression.xml'
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="contract"/></testsuite>')
        if 'help:effective-pom' in argv:
            output = Path(next(arg.removeprefix('-Doutput=') for arg in argv if arg.startswith('-Doutput=')))
            pom = (cwd / 'pom.xml').read_text()
            # A resolved managed dependency appears with its effective version.
            pom = pom.replace('<artifactId>shared</artifactId></dependency>', '<artifactId>shared</artifactId><version>2.0.0</version></dependency>')
            pom = pom.replace('<java.version>', '<maven.compiler.release>').replace('</java.version>', '</maven.compiler.release>')
            output.write_text(pom)
        if 'dependency:tree' in argv:
            destination = cwd / 'target/java-update-dependencies.tgf'
            destination.parent.mkdir(exist_ok=True)
            destination.write_text('1 example:app:jar:1\n2 org.example:shared:jar:2.0.0:compile\n#\n1 2\n')
        return ''

    @contextlib.contextmanager
    def external_tools(self):
        with patch('java_update_tool.cli.execute_migration', side_effect=self.fake_migration), patch('java_update_tool.validation.command', side_effect=self.fake_build):
            yield

    def test_manual_defaults_pause_display_resources_and_resume(self):
        self.config['workflow'] = {}
        self.save()
        code, output = self.invoke('run')
        self.assertEqual(3, code, output)
        root = self.run_root()
        self.assertIn(str(root / STAGES[0]), output)
        self.assertFalse((root / STAGES[1]).exists())
        self.assertEqual(0, self.invoke('approve', root.name, '--stage', STAGES[0])[0])
        code, output = self.invoke('run', '--resume', root.name)
        self.assertEqual(3, code, output)
        self.assertTrue((root / STAGES[1]).is_dir())
        self.assertFalse((root / STAGES[2]).exists())

    def test_cli_mode_override_and_runs_are_preserved(self):
        self.config['workflow'] = {}
        self.save()
        self.assertEqual(0, self.invoke('run', '--mode', 'unattended')[0])
        first = self.run_root()
        self.assertEqual(0, self.invoke('run', '--mode', 'unattended')[0])
        second = self.run_root()
        self.assertNotEqual(first, second)
        self.assertTrue((first / STAGES[2]).exists())
        code, output = self.invoke('diff', second.name, '--compare-to', first.name)
        self.assertEqual(0, code, output)
        self.assertTrue((second / 'reports' / f'diff-{first.name}.md').exists())

    def test_reviewed_artifact_change_invalidates_approval(self):
        self.config['workflow'] = {}
        self.save()
        self.invoke('run')
        root = self.run_root()
        self.invoke('approve', root.name, '--stage', STAGES[0])
        path = artifact_path(root, STAGES[0], 'repositories', 'api')
        value = read_json(path)
        value['summary']['java_versions'] = ['99']
        write_json(path, value)
        code, output = self.invoke('run', '--resume', root.name)
        self.assertEqual(2, code, output)
        self.assertIn('approval invalidated', output)
        self.assertNotIn(STAGES[0], read_json(root / 'run.json')['approvals'])

    def test_policy_change_requires_new_run(self):
        self.invoke('run')
        root = self.run_root()
        self.config['workflow']['show_diffs'] = True
        self.save()
        code, output = self.invoke('run', '--resume', root.name)
        self.assertEqual(2, code, output)
        self.assertIn('start a new run', output)

    def test_full_pipeline_publishes_inspectable_local_history(self):
        with self.external_tools():
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, output)
        root = self.run_root()
        result = read_json(artifact_path(root, STAGES[5], 'repositories', 'api'))
        local = Path(result['targets']['local_repo']['url'])
        self.assertEqual('main', git(local, 'branch', '--show-current'))
        git(local, 'merge-base', '--is-ancestor', self.original, 'HEAD')
        self.assertEqual('3', git(local, 'rev-list', '--count', 'HEAD'))
        self.assertEqual(self.original, git(self.source, 'rev-parse', 'HEAD'))
        validation = read_json(artifact_path(root, STAGES[4], 'repositories', 'api'))
        self.assertEqual('validated', validation['status'])
        self.assertEqual('2.0.0', validation['inventory']['dependencies'][0]['version'])
        self.assertTrue(any('help:effective-pom' in call for call in self.calls))

    def test_analyzed_engine_is_not_validated_or_published(self):
        def analyzed(argv):
            output = self.fake_migration(argv)
            workspace = Path(argv[argv.index('--workspace') + 1])
            write_json(workspace / 'reports/summary.json', {'results': [{'status': 'analyzed'}]})
            return output
        with patch('java_update_tool.cli.execute_migration', side_effect=analyzed):
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, output)
        self.assertFalse((self.run_root() / STAGES[4]).exists())
        self.assertEqual('analyzed', read_json(artifact_path(self.run_root(), STAGES[3], 'repositories', 'api'))['status'])

    def test_validation_failure_blocks_all_publishing(self):
        with patch('java_update_tool.cli.execute_migration', side_effect=self.fake_migration), patch('java_update_tool.validation.command', side_effect=PortfolioError('build failed')):
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(1, code, output)
        self.assertFalse((self.run_root() / STAGES[5]).exists())

    def test_changed_validated_checkout_cannot_publish(self):
        with self.external_tools():
            self.assertEqual(0, self.invoke('run', '--through', STAGES[4], '--execute')[0])
        root = self.run_root()
        value = read_json(artifact_path(root, STAGES[4], 'repositories', 'api'))
        (Path(value['output']) / 'notes.txt').write_text('Unvalidated changes')
        code, output = self.invoke('run', '--resume', root.name, '--through', STAGES[5], '--execute')
        self.assertEqual(2, code, output)
        self.assertIn('validated output changed', output)

    def test_pin_conflicts_and_downgrades_stop_planning(self):
        self.config['alignment']['dependencies']['pins'] = {'org.example:*': '2.0.0', '*:shared': '3.0.0'}
        self.save()
        code, output = self.invoke('run')
        self.assertEqual(2, code, output)
        self.assertIn('conflicting wildcard pins', output)
        self.config['alignment']['dependencies']['pins'] = {'org.example:shared': '0.5.0'}
        self.save()
        code, output = self.invoke('run')
        self.assertEqual(2, code, output)
        self.assertIn('downgrade requires', output)

    def test_exact_pin_overrides_wildcard_and_target_drives_recipe(self):
        self.config['alignment']['dependencies']['pins'] = {'org.example:*': '1.5.0', 'org.example:shared': '2.0.0'}
        self.save()
        self.assertEqual(0, self.invoke('run')[0])
        policy = read_json(self.run_root() / 'planned-policy.json')
        self.assertIn('org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_5', policy['migration']['openrewrite']['recipes'])

    def test_alias_collision_is_rejected(self):
        self.portfolio['repositories'][0]['repo_id'] = 'api-prod'
        self.portfolio['repositories'].append({'repo_name': 'worker', 'repo_id': 'api', 'source': str(self.source), 'application_id': 'orders', 'application_group_id': 'commerce'})
        self.save()
        code, output = self.invoke('validate')
        self.assertEqual(2, code, output)
        self.assertIn('ambiguous', output)

    def test_github_and_gitlab_create_destination_set_main_and_transfer_all_refs(self):
        for provider in ('github', 'gitlab'):
            with self.subTest(provider=provider):
                host = FakeHost(self.root, provider)
                try:
                    self.config['workflow']['publishing'] = {'enabled': True, 'targets': ['dst_repo'], 'provider': provider,
                        'api_url': host.url, 'token_env': 'TEST_HOST_TOKEN', 'owner': 'acme', 'prefix': provider + '-', 'history': 'all'}
                    self.save()
                    with self.external_tools(), patch.dict(os.environ, {'TEST_HOST_TOKEN': 'test-only-secret'}):
                        code, output = self.invoke('run', '--through', STAGES[5], '--execute')
                    self.assertEqual(0, code, output)
                    destination = self.root / (provider + '-api')
                    self.assertEqual('refs/heads/main', git(destination, 'symbolic-ref', 'HEAD'))
                    self.assertEqual(self.original, git(destination, 'rev-parse', 'maintenance'))
                    self.assertTrue(git(destination, 'rev-parse', 'v1'))
                    self.assertEqual('3', git(destination, 'rev-list', '--count', 'main'))
                    receipt = read_json(artifact_path(self.run_root(), STAGES[5], 'repositories', 'api'))
                    self.assertEqual('published', receipt['status'])
                    self.assertNotIn('test-only-secret', (self.run_root() / 'run.json').read_text())
                finally:
                    host.close()

    def test_partial_failure_retry_only_unfinished_targets(self):
        destination = self.root / 'destination.git'
        destination.mkdir()
        git(destination, 'init', '--bare')
        self.config['workflow']['publishing'] = {'enabled': True, 'targets': ['local_repo', 'src_repo', 'dst_repo'],
            'mode': 'precreated', 'repositories': {'api': {'url': str(destination)}}}
        self.save()
        pushed = []

        def transient(argv, cwd, timeout=300):
            if argv[:2] == ['git', 'push']:
                pushed.append(argv)
                if str(destination) in argv:
                    raise PortfolioError('temporary destination failure')
            return command(argv, cwd, timeout)

        with self.external_tools(), patch('java_update_tool.publishing.command', side_effect=transient):
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(1, code, output)
        root = self.run_root()
        first = read_json(artifact_path(root, STAGES[5], 'repositories', 'api'))
        self.assertEqual('published', first['targets']['src_repo']['status'])
        self.assertEqual('failed', first['targets']['dst_repo']['status'])
        self.assertTrue(git(self.source, 'rev-parse', read_json(self.run_root() / 'run.json')['sources']['api']['migration_branch']))
        pushed.clear()

        def record(argv, cwd, timeout=300):
            if argv[:2] == ['git', 'push']:
                pushed.append(argv)
            return command(argv, cwd, timeout)

        with patch('java_update_tool.publishing.command', side_effect=record):
            code, output = self.invoke('run', '--resume', root.name, '--from', STAGES[5], '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, output)
        self.assertEqual(1, len(pushed))
        self.assertIn(str(destination), pushed[0])


    def add_worker(self, *, independent=False):
        self.portfolio['repositories'].append({'repo_name': 'worker', 'source': str(self.source),
            'application_id': 'shipping' if independent else 'orders',
            'application_group_id': 'logistics' if independent else 'commerce', 'depends_on': ['api']})
        self.save()

    def test_outside_dependency_needs_evidence_and_inclusion_expands_scope(self):
        self.add_worker()
        with self.external_tools():
            code, output = self.invoke('run', '--repo', 'worker', '--through', STAGES[3], '--execute')
        self.assertEqual(2, code, output)
        self.assertIn('needs validated compatibility evidence', output)
        self.assertFalse(self.calls)
        with self.external_tools():
            code, output = self.invoke('run', '--repo', 'worker', '--include-dependencies', '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, output)
        self.assertEqual(['api', 'worker'], read_json(self.run_root() / 'run.json')['selected'])
        self.assertEqual(['api', 'worker'], [Path(call[2]).name for call in self.calls if '--policy' in call])

    def test_dependency_evidence_matches_policy_source_and_output(self):
        with self.external_tools():
            self.assertEqual(0, self.invoke('run', '--through', STAGES[4], '--execute')[0])
        evidence = artifact_path(self.run_root(), STAGES[4], 'repositories', 'api')
        self.add_worker(independent=True)
        self.config['workflow']['dependency_evidence'] = {'api': str(evidence)}
        self.save()
        with self.external_tools():
            code, output = self.invoke('run', '--repo', 'worker', '--through', STAGES[3], '--execute')
        self.assertEqual(0, code, output)
        (self.source / 'new.txt').write_text('Changed dependency source')
        git(self.source, 'add', '--all')
        git(self.source, 'commit', '-m', 'Move dependency source')
        with self.external_tools():
            code, output = self.invoke('run', '--repo', 'worker', '--through', STAGES[3], '--execute')
        self.assertEqual(2, code, output)
        self.assertIn('needs validated compatibility evidence', output)

    def test_manual_dependency_override_is_recorded_and_cannot_publish(self):
        self.add_worker(independent=True)
        self.config['workflow'] = {'mode': 'manual', 'checkpoints': [], 'allow_dependency_override': True}
        self.save()
        with self.external_tools():
            code, output = self.invoke('run', '--repo', 'worker', '--through', STAGES[5], '--execute')
        self.assertEqual(2, code, output)
        self.assertTrue(read_json(self.run_root() / 'run.json')['dependency_override'])
        self.assertIn('publishing is blocked', output)
        self.config['workflow']['mode'] = 'unattended'
        self.save()
        code, output = self.invoke('validate')
        self.assertEqual(2, code, output)
        self.assertIn('manual mode', output)

    def test_remote_shallow_requested_ref_publishes_complete_ancestry(self):
        self.portfolio['repositories'][0].update(source=self.source.as_uri(), ref=self.original)
        self.save()
        with self.external_tools():
            code, output = self.invoke('run', '--through', STAGES[4], '--execute')
        self.assertEqual(0, code, output)
        root = self.run_root()
        source = Path(read_json(root / 'run.json')['sources']['api']['snapshot'])
        self.assertEqual('true', git(source, 'rev-parse', '--is-shallow-repository'))
        code, output = self.invoke('run', '--resume', root.name, '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, output)
        local = root / 'local-repositories/api'
        self.assertEqual('false', git(local, 'rev-parse', '--is-shallow-repository'))
        self.assertEqual('3', git(local, 'rev-list', '--count', 'HEAD'))
        self.assertEqual('', git(local, 'tag', '--list'))

    def test_local_ref_is_honored_before_migration(self):
        self.portfolio['repositories'][0]['ref'] = 'v1'
        self.save()
        with self.external_tools():
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, output)
        receipt = read_json(self.run_root() / 'run.json')
        self.assertEqual(git(self.source, 'rev-parse', 'v1'), receipt['sources']['api']['commit'])
        local = self.run_root() / 'local-repositories/api'
        self.assertEqual('2', git(local, 'rev-list', '--count', 'HEAD'))
        self.assertFalse((local / 'notes.txt').exists())

    def test_final_wrong_pin_and_contract_failure_block_publishing(self):
        def wrong_pin(argv, cwd, timeout=300):
            result = self.fake_build(argv, cwd, timeout)
            if 'help:effective-pom' in argv:
                path = Path(next(arg.removeprefix('-Doutput=') for arg in argv if arg.startswith('-Doutput=')))
                path.write_text(path.read_text().replace('2.0.0', '9.0.0'))
            return result
        with patch('java_update_tool.cli.execute_migration', side_effect=self.fake_migration), patch('java_update_tool.validation.command', side_effect=wrong_pin):
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(1, code, output)
        validation = read_json(artifact_path(self.run_root(), STAGES[4], 'repositories', 'api'))
        self.assertIn('unmet pin', validation['error'])
        self.config['workflow']['validation'] = {'repositories': {'api': {'commands': [['contract-check']]}}}
        self.save()
        def contract(argv, cwd, timeout=300):
            if argv == ['contract-check']:
                raise PortfolioError('required contract check failed')
            return self.fake_build(argv, cwd, timeout)
        with patch('java_update_tool.cli.execute_migration', side_effect=self.fake_migration), patch('java_update_tool.validation.command', side_effect=contract):
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(1, code, output)
        validation = read_json(artifact_path(self.run_root(), STAGES[4], 'repositories', 'api'))
        self.assertIn('required contract check failed', validation['error'])

    def test_independent_publication_requires_manual_partial_validation_approval(self):
        self.portfolio['repositories'].append({'repo_name': 'bad', 'source': str(self.source),
            'application_id': 'broken', 'application_group_id': 'broken'})
        self.config['workflow'] = {'mode': 'manual', 'checkpoints': [STAGES[4]],
            'publishing': {'independent_applications': True}}
        self.save()
        def build(argv, cwd, timeout=300):
            if cwd.name == 'bad':
                raise PortfolioError('broken build')
            return self.fake_build(argv, cwd, timeout)
        with patch('java_update_tool.cli.execute_migration', side_effect=self.fake_migration), patch('java_update_tool.validation.command', side_effect=build):
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(3, code, output)
        root = self.run_root()
        self.assertEqual('partial', read_json(root / 'run.json')['stages'][STAGES[4]]['status'])
        self.assertFalse((root / STAGES[5]).exists())
        self.assertEqual(0, self.invoke('approve', root.name, '--stage', STAGES[4])[0])
        code, output = self.invoke('run', '--resume', root.name, '--through', STAGES[5], '--execute')
        self.assertEqual(1, code, output)
        self.assertTrue((root / 'local-repositories/api').exists())
        self.assertFalse((root / 'local-repositories/bad').exists())

    def test_remote_enablement_and_divergent_precreated_destination_are_gated(self):
        destination = self.root / 'destination.git'
        destination.mkdir()
        git(destination, 'init', '--bare')
        unrelated = self.root / 'unrelated'
        unrelated.mkdir()
        git(unrelated, 'init', '-b', 'main')
        (unrelated / 'keep.txt').write_text('Unrelated destination history')
        git(unrelated, 'add', '--all')
        git(unrelated, 'commit', '-m', 'Existing destination')
        git(unrelated, 'push', str(destination), 'HEAD:refs/heads/main')
        original = git(destination, 'rev-parse', 'main')
        self.config['workflow']['publishing'] = {'targets': ['dst_repo'], 'mode': 'precreated',
            'repositories': {'api': {'url': str(destination)}}}
        self.save()
        with self.external_tools():
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(2, code, output)
        self.assertIn('remote publishing requires', output)
        self.config['workflow']['publishing']['enabled'] = True
        self.save()
        with self.external_tools():
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(1, code, output)
        self.assertEqual(original, git(destination, 'rev-parse', 'main'))

    def test_auto_create_collision_does_not_reuse_destination(self):
        host = FakeHost(self.root, 'github')
        self.addCleanup(host.close)
        host.repos['modern-api'] = {'clone_url': str(self.root / 'existing')}
        self.config['workflow']['publishing'] = {'enabled': True, 'targets': ['dst_repo'],
            'owner': 'acme', 'prefix': 'modern-', 'api_url': host.url, 'token_env': 'TEST_HOST_TOKEN'}
        self.save()
        with self.external_tools(), patch.dict(os.environ, {'TEST_HOST_TOKEN': 'test-only-token'}):
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(1, code, output)
        result = read_json(artifact_path(self.run_root(), STAGES[5], 'repositories', 'api'))
        self.assertIn('destination already exists', result['targets']['dst_repo']['error'])
        self.assertFalse(any(call[0] == 'POST' for call in host.calls))

    def test_branch_mapping_collision_blocks_local_all_history(self):
        git(self.source, 'branch', 'main', 'v1')
        self.config['workflow']['publishing'] = {'history': 'all'}
        self.save()
        with self.external_tools():
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(1, code, output)
        result = read_json(artifact_path(self.run_root(), STAGES[5], 'repositories', 'api'))
        self.assertIn('branch mapping collision', result['targets']['local_repo']['error'])

    def test_reviewed_nested_assessment_drift_invalidates_approval(self):
        self.config['workflow'] = {'mode': 'manual', 'checkpoints': [STAGES[4]]}
        self.save()
        with self.external_tools():
            self.assertEqual(3, self.invoke('run', '--through', STAGES[5], '--execute')[0])
        root = self.run_root()
        self.assertEqual(0, self.invoke('approve', root.name, '--stage', STAGES[4])[0])
        path = artifact_path(root / STAGES[4] / 'assessment', STAGES[1], 'application-groups', 'commerce')
        value = read_json(path)
        value['status'] = 'attention-required'
        write_json(path, value)
        code, output = self.invoke('run', '--resume', root.name, '--through', STAGES[5], '--execute')
        self.assertEqual(2, code, output)
        self.assertIn('approval invalidated', output)

    def test_uncommitted_engine_output_cannot_validate_or_publish(self):
        def uncommitted(argv):
            result = self.fake_migration(argv)
            output = Path(argv[argv.index('--output') + 1]) / Path(argv[2]).name
            git(output, 'reset', '--soft', self.original)
            return result
        with patch('java_update_tool.cli.execute_migration', side_effect=uncommitted), patch('java_update_tool.validation.command', side_effect=self.fake_build):
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(1, code, output)
        self.assertFalse((self.run_root() / STAGES[5]).exists())
        self.assertIn('--commit', self.calls[0])

    def test_invalid_options_and_contradictory_recipes_fail_before_migration(self):
        cases = [({'publishing': {'repositories': {'api': {'unexpected': True}}}}, 'unknown repository'),
                 ({'publishing': {'api_url': 'https://user:secret@example.com'}}, 'must not contain credentials'),
                 ({'validation': {'commands': ['shell command']}}, 'argv lists')]
        for options, message in cases:
            self.config['workflow'] = options
            self.save()
            code, output = self.invoke('validate')
            self.assertEqual(2, code, output)
            self.assertIn(message, output)
        self.config['workflow'] = {}
        self.config['migration']['openrewrite']['recipes'] = ['org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0']
        self.save()
        code, output = self.invoke('validate')
        self.assertEqual(2, code, output)
        self.assertIn('contradicts', output)

    def test_resume_after_final_requested_planning_approval_finishes(self):
        self.config['workflow'] = {'mode': 'manual', 'checkpoints': [STAGES[2]]}
        self.save()
        self.assertEqual(3, self.invoke('run')[0])
        root = self.run_root()
        self.assertEqual(0, self.invoke('approve', root.name, '--stage', STAGES[2])[0])
        code, output = self.invoke('run', '--resume', root.name)
        self.assertEqual(0, code, output)
        self.assertEqual('complete', read_json(root / 'run.json')['status'])

    def test_failed_dependency_blocks_transitive_independent_publication(self):
        self.add_worker(independent=True)
        self.portfolio['repositories'].append({'repo_name': 'consumer', 'source': str(self.source),
            'application_id': 'consumer', 'application_group_id': 'consumer', 'depends_on': ['worker']})
        self.config['workflow']['publishing'] = {'independent_applications': True}
        self.save()
        def build(argv, cwd, timeout=300):
            if cwd.name == 'api':
                raise PortfolioError('failed dependency build')
            return self.fake_build(argv, cwd, timeout)
        with patch('java_update_tool.cli.execute_migration', side_effect=self.fake_migration), patch('java_update_tool.validation.command', side_effect=build):
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(1, code, output)
        root = self.run_root()
        for key in ('api', 'worker', 'consumer'):
            result = read_json(artifact_path(root, STAGES[5], 'repositories', key))
            self.assertEqual('blocked', result['status'])
        self.assertFalse((root / 'local-repositories').exists())

    def test_all_history_keeps_unselected_destination_refs(self):
        destination = self.root / 'destination.git'
        destination.mkdir()
        git(destination, 'init', '--bare')
        git(self.source, 'push', str(destination), 'HEAD:refs/heads/keep')
        self.config['workflow']['publishing'] = {'enabled': True, 'targets': ['dst_repo'], 'history': 'all',
            'mode': 'precreated', 'repositories': {'api': {'url': str(destination)}}}
        self.save()
        with self.external_tools():
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, output)
        self.assertEqual(self.original, git(destination, 'rev-parse', 'keep'))
        self.assertEqual('3', git(destination, 'rev-list', '--count', 'main'))

    def test_gradle_resolved_inventory_is_used_for_validation(self):
        (self.source / 'pom.xml').unlink()
        (self.source / 'build.gradle').write_text("plugins { id 'java' }\njava { targetCompatibility = JavaVersion.VERSION_17 }\n")
        git(self.source, 'add', '--all')
        git(self.source, 'commit', '-m', 'Use Gradle catalog fixture')
        def migrate(argv):
            source = Path(argv[2])
            output = Path(argv[argv.index('--output') + 1]) / source.name
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, output)
            build = output / 'build.gradle'
            build.write_text(build.read_text().replace('VERSION_17', 'VERSION_21'))
            git(output, 'add', '--all')
            git(output, 'commit', '-m', 'Update Gradle Java target')
            workspace = Path(argv[argv.index('--workspace') + 1])
            write_json(workspace / 'reports/summary.json', {'results': [{'status': 'changed'}]})
            return subprocess.CompletedProcess(argv, 0)
        def build(argv, cwd, timeout=300):
            if 'test' in argv:
                report = cwd / 'build/test-results/test/TEST-regression.xml'
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text('<testsuite tests="1"/>')
            if 'javaUpdateInventory' in argv:
                init = Path(argv[argv.index('--init-script') + 1])
                write_json(init.parent / 'effective-gradle.json', {'java_versions': ['21'], 'spring_boot_versions': [],
                    'dependencies': [{'coordinate': 'org.example:shared', 'group': 'org.example', 'artifact': 'shared', 'version': '2.0.0'}]})
            return ''
        with patch('java_update_tool.cli.execute_migration', side_effect=migrate), patch('java_update_tool.validation.command', side_effect=build):
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, output)
        validation = read_json(artifact_path(self.run_root(), STAGES[4], 'repositories', 'api'))
        self.assertEqual('2.0.0', validation['inventory']['dependencies'][0]['version'])

    def test_permitted_downgrade_and_missing_framework_artifact(self):
        self.config['alignment']['dependencies']['pins'] = {'org.example:shared': '0.5.0'}
        self.config['workflow']['allow_downgrades'] = True
        self.save()
        code, output = self.invoke('run')
        self.assertEqual(0, code, output)
        self.config['migration']['openrewrite']['artifacts'] = []
        self.save()
        code, output = self.invoke('run')
        self.assertEqual(2, code, output)
        self.assertIn('requires a pinned rewrite-spring artifact', output)

    def test_effective_unknown_dependency_version_blocks_validation(self):
        def build(argv, cwd, timeout=300):
            result = self.fake_build(argv, cwd, timeout)
            if 'help:effective-pom' in argv:
                output = Path(next(arg.removeprefix('-Doutput=') for arg in argv if arg.startswith('-Doutput=')))
                output.write_text(output.read_text().replace('2.0.0', '${unresolved.version}'))
            return result
        with patch('java_update_tool.cli.execute_migration', side_effect=self.fake_migration), patch('java_update_tool.validation.command', side_effect=build):
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(1, code, output)
        result = read_json(artifact_path(self.run_root(), STAGES[4], 'repositories', 'api'))
        self.assertIn('unresolved', result['error'])

    def test_repository_creation_receipt_survives_failed_push(self):
        host = FakeHost(self.root, 'github')
        self.addCleanup(host.close)
        self.config['workflow']['publishing'] = {'enabled': True, 'targets': ['dst_repo'], 'owner': 'acme',
            'prefix': 'created-', 'api_url': host.url, 'token_env': 'TEST_HOST_TOKEN'}
        self.save()
        def fail(argv, cwd, timeout=300):
            if argv[:2] == ['git', 'push']:
                raise PortfolioError('transient push failure')
            return command(argv, cwd, timeout)
        with self.external_tools(), patch.dict(os.environ, {'TEST_HOST_TOKEN': 'test-only-token'}), patch('java_update_tool.publishing.command', side_effect=fail):
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(1, code, output)
        root = self.run_root()
        receipt = read_json(artifact_path(root, STAGES[5], 'repositories', 'api'))
        self.assertIn('destination', receipt['targets']['dst_repo'])
        with patch.dict(os.environ, {'TEST_HOST_TOKEN': 'test-only-token'}):
            code, output = self.invoke('run', '--resume', root.name, '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, output)
        self.assertEqual(1, sum(call[0] == 'POST' for call in host.calls))

    def test_tracked_build_directory_source_changes_are_fingerprinted(self):
        build = self.source / 'src/main/java/build'
        build.mkdir(parents=True)
        (build / 'Service.java').write_text('package build; class Service {}')
        git(self.source, 'add', '--all')
        git(self.source, 'commit', '-m', 'Track a Java package named build')
        with self.external_tools():
            code, output = self.invoke('run', '--through', STAGES[4], '--execute')
        self.assertEqual(0, code, output)
        validation = read_json(artifact_path(self.run_root(), STAGES[4], 'repositories', 'api'))
        path = Path(validation['output']) / 'src/main/java/build/Service.java'
        path.write_text('package build; class Service { int unvalidated; }')
        code, output = self.invoke('run', '--resume', self.run_root().name, '--through', STAGES[5], '--execute')
        self.assertEqual(2, code, output)
        self.assertIn('validated output changed', output)

    def test_host_authentication_failure_retains_failed_target_without_push(self):
        host = FakeHost(self.root, 'github')
        self.addCleanup(host.close)
        host.authentication_failed = True
        self.config['workflow']['publishing'] = {'enabled': True, 'targets': ['dst_repo'], 'owner': 'acme',
            'prefix': 'auth-', 'api_url': host.url, 'token_env': 'TEST_HOST_TOKEN'}
        self.save()
        with self.external_tools(), patch.dict(os.environ, {'TEST_HOST_TOKEN': 'test-only-token'}):
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(1, code, output)
        root = self.run_root()
        value = read_json(artifact_path(root, STAGES[5], 'repositories', 'api'))
        self.assertIn('HTTP 401', value['targets']['dst_repo']['error'])
        self.assertFalse(any(call[0] == 'POST' for call in host.calls))
        for path in root.rglob('*.json'):
            self.assertNotIn('test-only-token', path.read_text())

    def test_explicit_source_default_rename_preserves_original_and_migration_refs(self):
        host = FakeHost(self.root, 'github')
        self.addCleanup(host.close)
        host.repos['source'] = {'clone_url': str(self.source)}
        url = 'ssh://git@127.0.0.1/acme/source.git'
        self.portfolio['repositories'][0]['source'] = url
        self.config['workflow']['publishing'] = {'enabled': True, 'targets': ['src_repo'], 'owner': 'acme',
            'source_default_branch': 'main', 'api_url': host.url, 'token_env': 'TEST_HOST_TOKEN'}
        self.save()
        def transport(argv, cwd, timeout=300):
            return command([str(self.source) if arg == url else arg for arg in argv], cwd, timeout)
        with self.external_tools(), patch.dict(os.environ, {'TEST_HOST_TOKEN': 'test-only-token'}), patch('java_update_tool.runs.command', side_effect=transport), patch('java_update_tool.publishing.command', side_effect=transport):
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, output)
        self.assertEqual('main', git(self.source, 'branch', '--show-current'))
        self.assertEqual(self.original, git(self.source, 'rev-parse', 'master'))
        self.assertEqual(self.original, git(self.source, 'rev-parse', 'main'))
        self.assertNotEqual(self.original, git(self.source, 'rev-parse', read_json(self.run_root() / 'run.json')['sources']['api']['migration_branch']))

    def test_stage_five_and_six_markdown_show_validation_and_target_results(self):
        with self.external_tools():
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, output)
        root = self.run_root()
        validation = (root / 'reports' / STAGES[4] / 'repositories/api/result.md').read_text()
        publishing = (root / 'reports' / STAGES[5] / 'repositories/api/result.md').read_text()
        self.assertIn('Status: **validated**', validation)
        self.assertIn('Resolved dependencies', validation)
        self.assertIn('local_repo', publishing)
        self.assertIn('published', publishing)

    def test_engine_preserves_tracked_build_paths_and_honors_linked_worktree_ref(self):
        import java_migrator as engine
        tracked = self.source / 'src/main/java/build'
        tracked.mkdir(parents=True)
        (tracked / 'Service.java').write_text('package build; class Service {}')
        git(self.source, 'add', '--all')
        git(self.source, 'commit', '-m', 'Build package is source')
        output = self.root / 'engine-output'
        workspace = self.root / 'engine-workspace'
        output.mkdir()
        workspace.mkdir()
        args = engine.parse_args([str(self.source), '--output', str(output), '--workspace', str(workspace)])
        checkout, _ = engine.prepare_repo(engine.RepoSpec(str(self.source)), args, os.environ.copy(),
                                         workspace / 'log.txt', engine.make_askpass(workspace))
        self.assertTrue((checkout / 'src/main/java/build/Service.java').exists())
        linked = self.root / 'linked'
        git(self.source, 'worktree', 'add', str(linked), 'maintenance')
        checkout, _ = engine.prepare_repo(engine.RepoSpec(str(linked), 'master'), args, os.environ.copy(),
                                         workspace / 'log.txt', engine.make_askpass(workspace))
        self.assertTrue((checkout / '.git').is_dir())
        self.assertEqual(git(self.source, 'rev-parse', 'master'), git(checkout, 'rev-parse', 'HEAD'))

    def test_bare_local_source_preserves_history(self):
        bare = self.root / 'source.git'
        command(['git', 'clone', '--bare', str(self.source), str(bare)], self.root)
        self.portfolio['repositories'][0]['source'] = str(bare)
        self.save()
        with self.external_tools():
            code, output = self.invoke('run', '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, output)
        self.assertEqual('3', git(self.run_root() / 'local-repositories/api', 'rev-list', '--count', 'HEAD'))


if __name__ == '__main__':
    unittest.main()
