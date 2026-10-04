import contextlib
import os
import unittest
from unittest.mock import patch

import test_workflow as fixtures
from java_update_tool.core import STAGES, artifact_path, read_json
from java_update_tool.runs import command, git


class ReviewTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    save = fixtures.WorkflowTests.save
    invoke = fixtures.WorkflowTests.invoke
    run_root = fixtures.WorkflowTests.run_root
    fake_migration = fixtures.WorkflowTests.fake_migration
    fake_build = fixtures.WorkflowTests.fake_build
    external_tools = fixtures.WorkflowTests.external_tools

    def host(self, provider):
        host = fixtures.FakeHost(self.root, provider)
        self.addCleanup(host.close)
        host.repos['source'] = {'clone_url': str(self.source)}
        self.url = 'ssh://git@127.0.0.1/acme/source.git'
        self.portfolio['repositories'][0]['source'] = self.url
        self.config['workflow']['publishing'] = {'enabled': True, 'targets': ['src_repo'], 'owner': 'acme',
             'provider': provider, 'api_url': host.url, 'token_env': 'TEST_HOST_TOKEN', 'request': {'enabled': True}}
        self.save()
        return host

    @contextlib.contextmanager
    def transport(self):
        def transport(argv, cwd, timeout=300):
            return command([str(self.source) if arg == self.url else arg for arg in argv], cwd, timeout)
        with self.external_tools(), patch.dict(os.environ, {'TEST_HOST_TOKEN': 'test-only-token'}), patch('java_update_tool.runs.command', side_effect=transport), patch('java_update_tool.publishing.command', side_effect=transport), patch('java_update_tool.reviews.command', side_effect=transport):
            yield

    def test_github_and_gitlab_drafts_preserve_readiness_and_stop_when_closed(self):
        for provider in ('github', 'gitlab'):
            with self.subTest(provider=provider):
                host = self.host(provider)
                with self.transport():
                    code, transcript = self.invoke('run', '--through', STAGES[5], '--execute')
                self.assertEqual(0, code, transcript)
                root = self.run_root()
                receipt = read_json(artifact_path(root, STAGES[5], 'repositories', 'api'))
                self.assertEqual('created', receipt['request']['status'])
                request = host.review_requests[0]
                self.assertTrue(request['draft'])
                request.update(draft=False, body='Human edited body', description='Human edited body')
                with self.transport():
                    code, transcript = self.invoke('run', '--resume', root.name, '--from', STAGES[5], '--through', STAGES[5], '--execute')
                self.assertEqual(0, code, transcript)
                self.assertEqual(1, len(host.review_requests))
                self.assertFalse(host.review_requests[0]['draft'])
                self.assertEqual('Human edited body', host.review_requests[0]['body'])
                request['state'] = 'closed'
                with self.transport():
                    code, _ = self.invoke('run', '--resume', root.name, '--from', STAGES[5], '--through', STAGES[5], '--execute')
                self.assertEqual(1, code)
                self.assertEqual(1, len(host.review_requests))
                receipt = read_json(artifact_path(root, STAGES[5], 'repositories', 'api'))
                self.assertEqual('published', receipt['targets']['src_repo']['status'])
                self.assertIn('closed', receipt['request']['error'])

    def test_ambiguous_create_reconciles_without_duplicate_or_repush(self):
        host = self.host('github')
        host.ambiguous_request = True
        with self.transport():
            self.assertEqual(1, self.invoke('run', '--through', STAGES[5], '--execute')[0])
        root = self.run_root()
        self.assertEqual(1, len(host.review_requests))
        with self.transport():
            code, transcript = self.invoke('run', '--resume', root.name, '--through', STAGES[5], '--execute')
        self.assertEqual(0, code, transcript)
        self.assertEqual(1, len(host.review_requests))
        self.assertEqual(1, sum(call[0] == 'POST' and call[1].endswith('/pulls') for call in host.calls))

    def test_base_change_requires_new_run_after_successful_source_push(self):
        host = self.host('github')
        with self.transport():
            self.assertEqual(0, self.invoke('run', '--through', STAGES[4], '--execute')[0])
        root = self.run_root()
        # All-ref drift is guarded separately. The captured explicit base check is
        # exercised by requesting a different base that advances after capture.
        from java_update_tool import reviews
        original = reviews.command
        def changed_base(argv, cwd, timeout=300):
            if 'ls-remote' in argv:
                return '0' * 40 + '\trefs/heads/master'
            return original(argv, cwd, timeout)
        with self.transport(), patch('java_update_tool.reviews.command', side_effect=changed_base):
            self.assertEqual(1, self.invoke('run', '--resume', root.name, '--through', STAGES[5], '--execute')[0])
        receipt = read_json(artifact_path(root, STAGES[5], 'repositories', 'api'))
        self.assertEqual('published', receipt['targets']['src_repo']['status'])
        self.assertIn('base commit changed', receipt['request']['error'])
        self.assertEqual([], host.review_requests)
