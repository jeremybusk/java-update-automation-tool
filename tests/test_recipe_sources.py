import json
import io
import contextlib
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from unittest.mock import patch
from pathlib import Path

import java_migrator as engine
from java_update_tool.cli import _migration_policy
from java_update_tool.recipe_sources import DEFAULT_LOCK, SourceBuildError, cache_valid, digest, source_order, validate_pom
from java_update_tool.recipe_binaries import SafeRedirect, try_repository


class RecipeSourceTests(unittest.TestCase):
    def test_locked_closure_builds_spring_and_its_dependencies_in_order(self):
        lock = json.loads(DEFAULT_LOCK.read_text())
        order = source_order(lock, ['org.openrewrite.recipe:rewrite-spring:6.40.0'])
        positions = {item['artifact'].split(':')[1]: index for index, item in enumerate(order)}
        self.assertEqual(18, len(order))
        for item in order:
            for dependency in item['depends_on']:
                self.assertLess(positions[dependency], positions[item['artifact'].split(':')[1]])
        with self.assertRaisesRegex(SourceBuildError, 'no entry'):
            source_order(lock, ['org.openrewrite.recipe:rewrite-spring:999.0.0'])
        lock['modules'][0]['depends_on'] = [lock['modules'][0]['artifact'].split(':')[1]]
        with self.assertRaisesRegex(SourceBuildError, 'cycle'):
            source_order(lock, [lock['modules'][0]['artifact']])

    def test_missing_or_changed_cache_artifact_invalidates_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jar, receipt = root / 'recipe.jar', root / 'receipt.json'
            self.assertFalse(cache_valid(receipt, root))
            jar.write_bytes(b'first source build')
            receipt.write_text(json.dumps({'files': {'recipe.jar': digest(jar)}}))
            self.assertTrue(cache_valid(receipt, root))
            jar.write_bytes(b'changed build')
            self.assertFalse(cache_valid(receipt, root))
            jar.unlink()
            self.assertFalse(cache_valid(receipt, root))

    def test_published_runtime_dependency_cannot_use_core_version_placeholder(self):
        with tempfile.TemporaryDirectory() as temporary:
            pom = Path(temporary) / 'recipe.pom'
            text = '''<project xmlns="http://maven.apache.org/POM/4.0.0"><dependencies><dependency>
              <groupId>tech.picnic.error-prone-support</groupId><artifactId>error-prone-contrib</artifactId>
              <version>VERSION</version><classifier>recipes</classifier><scope>runtime</scope>
              </dependency></dependencies></project>'''
            lock = json.loads(DEFAULT_LOCK.read_text())
            pom.write_text(text.replace('VERSION', '8.92.17'))
            with self.assertRaisesRegex(SourceBuildError, 'expected 0.30.0'):
                validate_pom(pom, lock)
            pom.write_text(text.replace('VERSION', '0.30.0'))
            validate_pom(pom, lock)

    def test_global_nexus_settings_reach_both_build_tools_without_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rewrite = {'recipe_repository': 'source', 'artifact_repository': 'https://nexus.example/repository/group/',
                       'source_cache': str(root / 'cache'), 'source_lock': str(DEFAULT_LOCK),
                       'repository_username_env': 'NEXUS_USERNAME', 'repository_token_env': 'NEXUS_PASSWORD',
                       'artifacts': ['org.openrewrite.recipe:rewrite-spring:6.40.0']}
            config = {'targets': {'java': {'desired': 21}}, 'migration': {'openrewrite': rewrite}}
            policy = root / 'policy.json'
            policy.write_text(json.dumps(_migration_policy(config)))
            args = engine.parse_args(['example', '--policy', str(policy), '--recipe-repository', 'source'])
            self.assertEqual(rewrite['artifact_repository'], args.artifact_repository)
            self.assertEqual(root / 'cache', args.recipe_cache)
            self.assertEqual('6.49.0', args.maven_plugin_version)
            args.source_maven_repository = root / 'built-recipes'
            args.maven_settings = root / 'missing-settings.xml'
            settings, init = root / 'settings.xml', root / 'init.gradle'
            engine.write_maven_settings(settings, args, {'NEXUS_USERNAME': 'internal-user', 'NEXUS_PASSWORD': 'private-secret'})
            engine.write_gradle_init(init, args, 'example.Recipe', root / 'rewrite.yml')
            for text in (settings.read_text(), init.read_text()):
                self.assertIn(rewrite['artifact_repository'], text)
                self.assertIn('NEXUS_PASSWORD', text)
                self.assertNotIn('private-secret', text)
                self.assertNotIn('internal-user', text)
                self.assertNotIn('artifacts.codegenomeproject.org', text)
            self.assertIn('<localRepository>' + str(args.source_maven_repository), settings.read_text())
            self.assertLess(init.read_text().index(args.source_maven_repository.as_uri()), init.read_text().index('nexus.example'))

    def test_auto_caches_binary_graph_and_falls_back_on_download_denial(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / 'lock.json'
            artifact = 'org.openrewrite.recipe:example:1.0'
            lock.write_text(json.dumps({'modules': [{'artifact': artifact, 'depends_on': []}]}))
            jar = io.BytesIO()
            with zipfile.ZipFile(jar, 'w') as archive:
                archive.writestr('META-INF/example.txt', 'binary')
            def response(request, timeout):
                return io.BytesIO(b'<project/>' if request.full_url.endswith('.pom') else jar.getvalue())
            with patch('urllib.request.OpenerDirector.open', side_effect=response) as download, contextlib.redirect_stdout(io.StringIO()):
                repository = try_repository([artifact], cache=root / 'cache', lock_path=lock)
                self.assertIsNotNone(repository)
                self.assertEqual(2, download.call_count)
                self.assertEqual(repository, try_repository([artifact], cache=root / 'cache', lock_path=lock))
                self.assertEqual(2, download.call_count)
                extra = 'org.openrewrite.recipe:other:1.0'
                data = json.loads(lock.read_text())
                data['modules'].append({'artifact': extra, 'depends_on': []})
                lock.write_text(json.dumps(data))
                first = try_repository([artifact], cache=root / 'cache', lock_path=lock)
                both = try_repository([artifact, extra], cache=root / 'cache', lock_path=lock)
                self.assertNotEqual(first, both)
                self.assertEqual(8, download.call_count)
            error = urllib.error.HTTPError('https://cache.example/recipe.jar', 403, 'Forbidden', {}, None)
            with patch('urllib.request.OpenerDirector.open', side_effect=error), contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(try_repository([artifact], cache=root / 'other-cache', lock_path=lock))

    def test_redirect_drops_credentials_when_origin_changes(self):
        redirect = SafeRedirect()
        request = urllib.request.Request('https://repository.example/a', headers={'Authorization': 'Basic private-secret'})
        same = redirect.redirect_request(request, None, 302, '', {}, 'https://repository.example/b')
        other = redirect.redirect_request(request, None, 302, '', {}, 'https://cdn.example/b')
        self.assertEqual('Basic private-secret', same.get_header('Authorization'))
        self.assertIsNone(other.get_header('Authorization'))

    def test_auto_does_not_rebuild_sources_after_recipe_execution_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch('java_update_tool.recipe_binaries.try_repository', return_value=root / 'binary-cache'), \
                 patch('java_update_tool.recipe_sources.prepare', return_value=root / 'plugin-cache') as build, \
                 patch.object(engine, 'migrate_one', return_value=engine.Result('example', status='failed', error='recipe execution failed')), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = engine.main(['example', '--recipe-repository', 'auto', '--workspace', str(root / 'work'),
                                      '--output', str(root / 'output')])
            self.assertEqual(1, result)
            self.assertEqual(1, build.call_count)
            self.assertEqual([], build.call_args.args[0])  # Only the compatibility plugin, before execution.


if __name__ == '__main__':
    unittest.main()
