"""Migration commits exclude build output even when inputs have no ignore file."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import java_migrator as engine
from java_update_tool.runs import git


class BuildOutputTests(unittest.TestCase):
    def test_migration_commits_source_but_not_root_or_module_outputs(self):
        for tool in ('maven', 'gradle'):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / 'source'
                module = source / 'module[1]'
                module.mkdir(parents=True)
                if tool == 'maven':
                    (source / 'pom.xml').write_text('<project><modules><module>module[1]</module></modules></project>')
                    (module / 'pom.xml').write_text('<project/>')
                else:
                    # A Gradle subproject need not have its own build file.
                    (source / 'settings.gradle').write_text("include 'module[1]'\n")
                    (source / 'build.gradle').write_text("plugins { id 'java' }\n")
                package = source / 'src/main/java/build'
                package.mkdir(parents=True)
                (package / 'Legacy.java').write_text('class Legacy {}\n')
                output_name = 'target' if tool == 'maven' else 'build'
                tracked = source / output_name / 'tracked.txt'
                tracked.parent.mkdir()
                tracked.write_text('original\n')
                git(source, 'init', '-b', 'main')
                git(source, 'add', '--all')
                git(source, 'commit', '-m', 'Original source')
                original = git(source, 'rev-parse', 'HEAD')
                (source / '.git/info/exclude').write_text('/existing-ignore/\n')
                workspace = root / 'workspace'
                workspace.mkdir()
                args = engine.parse_args([str(source), '--workspace', str(workspace),
                                          '--output', str(root / 'output'), '--commit'])

                def migrate(build, *_):
                    for directory in (build.path, build.path / module.name):
                        report = directory / output_name / 'test-results/TEST-fresh.xml'
                        report.parent.mkdir(parents=True, exist_ok=True)
                        report.write_text('<testsuite tests="1"/>')
                        if tool == 'gradle':
                            cache = directory / '.gradle/cache.bin'
                            cache.parent.mkdir()
                            cache.write_bytes(b'generated')
                    code = build.path / 'src/main/java/build'
                    (code / 'Legacy.java').write_text('class Legacy { int modern; }\n')
                    (code / 'New.java').write_text('class New {}\n')
                    (build.path / output_name / 'tracked.txt').write_text('updated\n')
                    return engine.ProjectResult(str(build.path), tool, status='changed', changed=True)

                with patch('java_migrator.migrate_project', side_effect=migrate):
                    result = engine.migrate_one(engine.RepoSpec(str(source)), args, os.environ.copy(),
                                                engine.make_askpass(workspace))
                self.assertEqual('changed', result.status, result.error)
                output = Path(result.path)
                files = git(output, 'ls-files').splitlines()
                self.assertFalse(any('TEST-fresh.xml' in name or name.endswith('cache.bin') for name in files))
                self.assertIn('src/main/java/build/New.java', files)
                self.assertEqual('updated\n', (output / output_name / 'tracked.txt').read_text())
                self.assertIn('updated', git(output, 'show', 'HEAD:' + output_name + '/tracked.txt'))
                self.assertEqual('', git(output, 'status', '--porcelain'))
                self.assertFalse((output / '.gitignore').exists())
                self.assertIn('/existing-ignore/', (output / '.git/info/exclude').read_text())
                git(output, 'merge-base', '--is-ancestor', original, 'HEAD')
                self.assertEqual(original, git(source, 'rev-parse', 'HEAD'))

    def test_generated_output_alone_is_not_a_source_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / 'source'
            repo.mkdir()
            (repo / 'pom.xml').write_text('<project/>')
            git(repo, 'init', '-b', 'main')
            git(repo, 'add', '--all')
            git(repo, 'commit', '-m', 'Original source')
            build = engine.BuildRoot(repo, 'maven')
            engine.ignore_build_outputs(repo, [build], [])
            args = engine.parse_args([str(repo)])
            state = root / 'state'
            state.mkdir()

            def build_output(*_, **__):
                report = repo / 'target/surefire-reports/TEST-fresh.xml'
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text('<testsuite tests="1"/>')

            with patch('java_migrator.analyze_project', return_value=engine.ProjectAnalysis()), \
                 patch('java_migrator.migration_phases', return_value=[engine.MigrationPhase('java', ('test.Recipe',))]), \
                 patch('java_migrator.run', side_effect=build_output), \
                 patch('java_migrator.verify_command', return_value=None), \
                 patch('java_migrator.run_post_checks', return_value=[]):
                result = engine.migrate_project(build, args, os.environ.copy(), root / 'engine.log', state)
            self.assertEqual('unchanged', result.status, result.error)
            self.assertFalse(result.changed)
            self.assertFalse(result.phases[0].changed)
