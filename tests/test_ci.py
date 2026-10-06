import contextlib
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts import run_ci_integration as ci
from scripts import setup_ci_maven as maven


class CiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = io.StringIO()

    def archive(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            data = b'#!/bin/sh\n'
            entry = tarfile.TarInfo(f'apache-maven-{maven.VERSION}/bin/mvn')
            entry.size = len(data)
            entry.mode = 0o755
            archive.addfile(entry, io.BytesIO(data))
        return stream.getvalue()

    def test_maven_cache_hit_skips_archive_download(self):
        data = self.archive()
        checksum = hashlib.sha512(data).hexdigest().encode()
        with patch('scripts.setup_ci_maven.urllib.request.urlopen',
                   side_effect=[io.BytesIO(checksum), io.BytesIO(data), io.BytesIO(checksum)]) as download:
            first = maven.install(self.root / 'cache', self.root / 'first')
            second = maven.install(self.root / 'cache', self.root / 'second')
        self.assertTrue((first / 'mvn').is_file())
        self.assertTrue((second / 'mvn').is_file())
        self.assertEqual([maven.URL + '.sha512', maven.URL, maven.URL + '.sha512'],
                         [call.args[0] for call in download.call_args_list])

    def test_corrupt_cached_maven_archive_is_replaced(self):
        cache = self.root / 'cache'
        cache.mkdir()
        archive = cache / f'apache-maven-{maven.VERSION}-bin.tar.gz'
        archive.write_bytes(b'corrupt cached archive')
        data = self.archive()
        checksum = hashlib.sha512(data).hexdigest().encode()
        with patch('scripts.setup_ci_maven.urllib.request.urlopen',
                   side_effect=[io.BytesIO(checksum), io.BytesIO(data)]):
            installed = maven.install(cache, self.root / 'installed')
        self.assertEqual(data, archive.read_bytes())
        self.assertTrue((installed / 'mvn').is_file())

    def test_maven_checksum_failure_is_not_cached(self):
        with patch('scripts.setup_ci_maven.urllib.request.urlopen',
                   side_effect=[io.BytesIO(b'wrong-checksum'), io.BytesIO(self.archive())]):
            with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                maven.install(self.root / 'cache', self.root / 'installed')
        self.assertEqual([], list((self.root / 'cache').iterdir()))
        self.assertFalse((self.root / 'installed').exists())

    def environment(self):
        return {'JAVA_UPDATE_ARTIFACTS': str(self.root / 'evidence'),
                'JAVA_UPDATE_CASE': 'unchanged-parent',
                'GITHUB_STEP_SUMMARY': str(self.root / 'summary.md')}

    def test_parallel_cases_keep_isolated_environments_and_report_failures(self):
        barrier = threading.Barrier(2)
        seen = []
        def run(argv, **kwargs):
            case = kwargs['env']['JAVA_UPDATE_CASE']
            seen.append(case)
            self.assertEqual('1', kwargs['env']['JAVA_UPDATE_INTEGRATION'])
            barrier.wait(timeout=5)
            kwargs['stdout'].write(f'{case} diagnostics\n')
            return subprocess.CompletedProcess(argv, 1 if case == 'gradle17' else 0)
        with patch.dict(os.environ, self.environment()), \
             patch('scripts.run_ci_integration.subprocess.run', side_effect=run), \
             contextlib.redirect_stdout(self.output):
            self.assertEqual(1, ci.main(['--jobs', '2', 'maven17', 'gradle17']))
            self.assertEqual('unchanged-parent', os.environ['JAVA_UPDATE_CASE'])
        self.assertEqual({'maven17', 'gradle17'}, set(seen))
        summary = (self.root / 'summary.md').read_text()
        self.assertIn('| maven17 | passed |', summary)
        self.assertIn('| gradle17 | failed |', summary)
        self.assertIn('gradle17 diagnostics', self.output.getvalue())
        for case in seen:
            self.assertTrue((self.root / f'evidence/logs/{case}.log').is_file())

    def test_recipe_preparation_runs_once_before_all_boot_cases(self):
        cases = ['boot35-maven', 'boot4-maven', 'boot35-gradle', 'boot4-gradle']
        seen = []
        def run(argv, **kwargs):
            name = 'prepare' if 'scripts/build_recipe_sources.py' in argv else kwargs['env']['JAVA_UPDATE_CASE']
            seen.append(name)
            self.assertEqual('prepare', seen[0])
            return subprocess.CompletedProcess(argv, 0)
        with patch.dict(os.environ, self.environment()), \
             patch('scripts.run_ci_integration.subprocess.run', side_effect=run), \
             contextlib.redirect_stdout(self.output):
            self.assertEqual(0, ci.main(['--prepare-recipes', '--jobs', '2', *cases]))
        self.assertEqual(1, seen.count('prepare'))
        self.assertEqual(set(cases), set(seen[1:]))
        self.assertIn('| recipe preparation | passed |', (self.root / 'summary.md').read_text())

    def test_failed_recipe_preparation_blocks_cases_and_records_timing(self):
        with patch.dict(os.environ, self.environment()), \
             patch('scripts.run_ci_integration.subprocess.run', return_value=subprocess.CompletedProcess([], 7)) as run:
            self.assertEqual(7, ci.main(['--prepare-recipes', 'boot4-maven']))
        self.assertEqual(1, run.call_count)
        self.assertIn('scripts/build_recipe_sources.py', run.call_args.args[0])
        self.assertIn('| recipe preparation | failed |', (self.root / 'summary.md').read_text())

    def test_binary_recipe_mode_skips_source_preparation(self):
        config = {'migration': {'openrewrite': {'recipe_repository': 'maven-central'}}}
        with patch.dict(os.environ, self.environment()), \
             patch('scripts.run_ci_integration.yaml.safe_load', return_value=config), \
             patch('scripts.run_ci_integration.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run, \
             contextlib.redirect_stdout(self.output):
            self.assertEqual(0, ci.main(['--prepare-recipes', 'boot4-maven']))
        self.assertEqual(1, run.call_count)
        self.assertIn('test_integration.py', run.call_args.args[0])

    def test_invalid_parallelism_or_duplicate_cases_stops_before_execution(self):
        for args in (['--jobs', '0', 'maven17'], ['maven17', 'maven17']):
            with self.subTest(args=args), contextlib.redirect_stderr(self.output), \
                 patch('scripts.run_ci_integration.subprocess.run') as run:
                with self.assertRaises(SystemExit) as error:
                    ci.main(args)
                self.assertEqual(2, error.exception.code)
                run.assert_not_called()
