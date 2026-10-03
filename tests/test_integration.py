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


@unittest.skipUnless(os.environ.get('JAVA_UPDATE_INTEGRATION') == '1', 'set JAVA_UPDATE_INTEGRATION=1 for real toolchains')
class RealMigrationTests(unittest.TestCase):
    def migrate(self, tool):
        if not shutil.which('java') or not shutil.which('mvn' if tool == 'maven' else 'gradle'):
            self.skipTest(f'JDK and {tool} must be installed')
        with tempfile.TemporaryDirectory(prefix='java-update-integration-') as temporary:
            root = Path(temporary)
            source = root / 'service'
            source.mkdir()
            (source / '.gitignore').write_text('target/\nbuild/\n.gradle/\n')
            if tool == 'maven':
                (source / 'pom.xml').write_text('''<project xmlns="http://maven.apache.org/POM/4.0.0">
<modelVersion>4.0.0</modelVersion><groupId>example</groupId><artifactId>service</artifactId><version>1</version>
<properties><maven.compiler.release>17</maven.compiler.release></properties>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-compiler-plugin</artifactId><version>3.13.0</version></plugin></plugins></build></project>''')
            else:
                (source / 'settings.gradle').write_text("rootProject.name = 'service'\n")
                (source / 'build.gradle').write_text("plugins { id 'java' }\nrepositories { mavenCentral() }\njava { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }\ndependencies { testImplementation 'junit:junit:4.13.2' }\n")
            package = source / 'src/main/java/example'
            package.mkdir(parents=True)
            (package / 'Service.java').write_text('package example; public class Service { public String value() { return "ok"; } }\n')
            tests = source / 'src/test/java/example'
            tests.mkdir(parents=True)
            (tests / 'ServiceTest.java').write_text('package example; import org.junit.Test; import static org.junit.Assert.assertEquals; public class ServiceTest { @Test public void contract() { assertEquals("ok", new Service().value()); } }\n')
            git(source, 'init', '-b', 'master')
            git(source, 'add', '--all')
            git(source, 'commit', '-m', 'Java 17 service')
            original = git(source, 'rev-parse', 'HEAD')
            config = {'schema_version': 1, 'targets': {'java': {'desired': 21, 'acceptable': [21]},
                      'spring_boot': {'desired': '3.5.x', 'acceptable': ['3.5.x']}},
                      'migration': {'profile': 'conservative', 'openrewrite': {'recipe_repository': 'maven-central'}},
                      'workflow': {'mode': 'unattended'}}
            portfolio = {'schema_version': 1, 'repositories': [{'repo_name': 'service', 'source': str(source),
                         'application_id': 'service', 'application_group_id': 'example'}]}
            config_path, portfolio_path, state = root / 'java-update.yml', root / 'repositories.yml', root / 'state'
            config_path.write_text(yaml.safe_dump(config))
            portfolio_path.write_text(yaml.safe_dump(portfolio))
            transcript = io.StringIO()
            with contextlib.redirect_stdout(transcript), contextlib.redirect_stderr(transcript):
                result = main(['--config', str(config_path), '--portfolio', str(portfolio_path), '--state', str(state),
                               'run', '--through', STAGES[5], '--execute'])
            self.assertEqual(0, result, transcript.getvalue())
            run = run_directory(state)
            validation = read_json(artifact_path(run, STAGES[4], 'repositories', 'service'))
            self.assertEqual('validated', validation['status'])
            self.assertEqual(['21'], validation['inventory']['java_versions'])
            self.assertTrue(validation['inventory']['dependencies'])
            local = run / 'local-repositories/service'
            self.assertNotEqual(original, git(local, 'rev-parse', 'HEAD'))
            git(local, 'merge-base', '--is-ancestor', original, 'HEAD')
            self.assertEqual(original, git(source, 'rev-parse', 'HEAD'))

    def test_real_maven_migration_and_resolved_validation(self):
        self.migrate('maven')

    def test_real_gradle_migration_and_resolved_validation(self):
        self.migrate('gradle')
