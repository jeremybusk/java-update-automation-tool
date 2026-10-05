import argparse
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import java_migrator as jm


class MigratorTests(unittest.TestCase):
    def test_sanitized_url_removes_embedded_credentials(self):
        self.assertEqual(
            "https://github.com/acme/app.git",
            jm.sanitized_url("https://user:secret@github.com/acme/app.git"),
        )

    def test_current_directory_has_a_destination_name(self):
        self.assertEqual(Path.cwd().name, jm.destination_name("."))

    def test_manifest_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = root / "repos.txt"
            text.write_text("# comment\nhttps://one/repo.git main\nhttps://two/repo.git\n")
            self.assertEqual("main", jm.read_manifest(text)[0].ref)
            data = root / "repos.json"
            data.write_text(json.dumps([
                "https://one/a.git",
                {"url": "https://two/b.git", "ref": "dev"},
            ]))
            self.assertEqual("dev", jm.read_manifest(data)[1].ref)

    def test_discovers_roots_but_not_nested_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "maven" / "module").mkdir(parents=True)
            (root / "maven" / "pom.xml").write_text("<project><modules><module>module</module></modules></project>")
            (root / "maven" / "module" / "pom.xml").write_text("<project/>")
            (root / "gradle").mkdir()
            (root / "gradle" / "settings.gradle").write_text("")
            builds = jm.discover_builds(root, "auto", 4)
            self.assertEqual(
                {(root / "maven", "maven"), (root / "gradle", "gradle")},
                {(item.path, item.tool) for item in builds},
            )

    def test_gradle_projects_and_composite_builds_have_distinct_membership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('app', 'included', 'independent'):
                (root / name).mkdir()
                (root / name / 'build.gradle').write_text("plugins { id 'java' }")
            (root / 'settings.gradle').write_text("include 'app'\nincludeBuild('included')\n")
            memberships = []
            builds = jm.discover_builds(root, 'auto', 4, memberships=memberships)
            self.assertEqual({'.', 'included', 'independent'}, {str(item.path.relative_to(root)) for item in builds})
            self.assertEqual({('app', 'project'), ('included', 'included-build')}, {(item['member'], item['relationship']) for item in memberships})

    def test_retained_maven_settings_reference_process_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / 'user-settings.xml', root / 'retained-settings.xml'
            source.write_text('<settings><servers><server><id>private</id><username>private-user</username><password>private-secret-value</password></server></servers></settings>')
            for repository in ('maven-central', 'codegenome'):
                args = jm.parse_args(['example', '--maven-settings', str(source), '--recipe-repository', repository])
                env = {'CODE_GENOME_USERNAME': 'recipe-user', 'CODE_GENOME_TOKEN': 'recipe-secret-value'}
                jm.write_maven_settings(destination, args, env)
                retained = destination.read_text()
                self.assertNotIn('private-user', retained)
                self.assertNotIn('private-secret-value', retained)
                self.assertNotIn('recipe-secret-value', retained)
                self.assertIn('private-secret-value', env.values())
                self.assertIn('${env.', retained)
                self.assertEqual(0o600, destination.stat().st_mode & 0o777)

    def test_namespaced_maven_settings_preserve_default_namespace_and_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / 'user-settings.xml', root / 'retained-settings.xml'
            xsi = 'http://www.w3.org/2001/XMLSchema-instance'
            for version in ('1.0.0', '1.1.0'):
                namespace = f'http://maven.apache.org/SETTINGS/{version}'
                schema = f'{namespace} https://maven.apache.org/xsd/settings-{version}.xsd'
                source.write_text(
                    f'<settings xmlns="{namespace}" xmlns:xsi="{xsi}" xsi:schemaLocation="{schema}">'
                    '<servers><server><id>private</id><username>private-user</username>'
                    '<password>private-secret-value</password></server></servers>'
                    '<mirrors><mirror><id>internal</id><mirrorOf>central</mirrorOf>'
                    '<url>https://example.com/maven</url></mirror></mirrors></settings>'
                )
                for repository in ('maven-central', 'codegenome'):
                    with self.subTest(version=version, repository=repository):
                        args = jm.parse_args(['example', '--maven-settings', str(source), '--recipe-repository', repository])
                        env = {'CODE_GENOME_USERNAME': 'recipe-user', 'CODE_GENOME_TOKEN': 'recipe-secret-value'}
                        jm.write_maven_settings(destination, args, env)
                        retained = destination.read_text()
                        self.assertTrue(retained.split('?>', 1)[1].lstrip().startswith('<settings '), retained)
                        parsed = ET.fromstring(retained)
                        ns = {'s': namespace}
                        self.assertEqual(f'{{{namespace}}}settings', parsed.tag)
                        self.assertEqual(schema, parsed.get(f'{{{xsi}}}schemaLocation'))
                        self.assertEqual('central', parsed.findtext('s:mirrors/s:mirror/s:mirrorOf', namespaces=ns))
                        self.assertEqual('https://example.com/maven', parsed.findtext('s:mirrors/s:mirror/s:url', namespaces=ns))
                        for secret in ('private-user', 'private-secret-value', 'recipe-secret-value'):
                            self.assertNotIn(secret, retained)
                        self.assertIn('private-secret-value', env.values())
                        self.assertEqual(0o600, destination.stat().st_mode & 0o777)
                        if repository == 'codegenome':
                            profile = parsed.find("s:profiles/s:profile[s:id='java-migrator-recipes']", ns)
                            self.assertIsNotNone(profile)
                            for collection, item in (('repositories', 'repository'), ('pluginRepositories', 'pluginRepository')):
                                self.assertEqual(jm.CODE_GENOME_URL, profile.findtext(f's:{collection}/s:{item}/s:url', namespaces=ns))
                            self.assertEqual('java-migrator-recipes', parsed.findtext('s:activeProfiles/s:activeProfile', namespaces=ns))
                            server = parsed.find("s:servers/s:server[s:id='codegenome']", ns)
                            self.assertIsNotNone(server)
                            self.assertEqual('${env.CODE_GENOME_USERNAME}', server.findtext('s:username', namespaces=ns))
                            self.assertEqual('${env.CODE_GENOME_TOKEN}', server.findtext('s:password', namespaces=ns))
                        else:
                            self.assertNotIn(jm.CODE_GENOME_URL, retained)

    def test_generated_recipe_contains_phase_recipes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rewrite.yml"
            phase = jm.MigrationPhase(
                "java",
                (
                    "org.openrewrite.java.migrate.UpgradeToJava21",
                    "org.openrewrite.java.migrate.UpgradeDockerImageVersion:\n      version: 21",
                ),
            )
            name = jm.write_recipe(path, phase)
            content = path.read_text()
            self.assertEqual("com.acme.migration.Java", name)
            self.assertIn("UpgradeToJava21", content)
            self.assertIn("UpgradeDockerImageVersion", content)

    def test_analysis_drives_recipe_packs_and_excludes_generated_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src" / "main" / "java").mkdir(parents=True)
            (root / "src" / "generated").mkdir(parents=True)
            (root / "build.gradle").write_text(
                "implementation 'org.projectlombok:lombok:1.18.20'\n"
                "annotationProcessor 'org.mapstruct:mapstruct-processor:1.4.2.Final'\n"
                "testImplementation 'junit:junit:4.12'\n"
                "testImplementation 'org.mockito:mockito-core:4.11.0'\n"
            )
            (root / "src" / "main" / "java" / "Legacy.java").write_text(
                "import sun.misc.Unsafe; import org.junit.Test; class Legacy {}\n"
            )
            (root / "src" / "generated" / "Generated.java").write_text(
                "import javax.xml.bind.JAXBContext; class Generated {}\n"
            )
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / ".github" / "workflows" / "build.yml").write_text("name: build\n")
            args = jm.parse_args([str(root)])
            build = jm.BuildRoot(root, "gradle")
            analysis = jm.analyze_project(build, args)
            self.assertTrue({"lombok", "mapstruct", "junit", "mockito", "jdk-internals"}
                            <= set(analysis.features))
            self.assertNotIn("removed-jdk-modules", analysis.features)
            self.assertIn("src/generated", analysis.excluded_paths)
            self.assertEqual([".github/workflows/build.yml"], analysis.external_configuration)
            self.assertTrue(any("External CI/toolchain" in item for item in analysis.findings))

            phases = jm.migration_phases(build, analysis, args)
            names = [phase.name for phase in phases]
            self.assertEqual(
                ["java", "compatibility", "testing-migration", "testing-cleanup",
                 "dependencies", "cleanup"],
                names,
            )
            recipes = "\n".join(recipe for phase in phases for recipe in phase.recipes)
            self.assertIn("AddLombokMapstructBinding", recipes)
            self.assertIn("Mockito4to5Only", recipes)
            self.assertIn("junit-platform-launcher", recipes)
            self.assertNotIn("junit:junit", recipes)

    def test_dependency_policy_skips_stacks_unless_pinned(self):
        args = jm.parse_args([
            "example", "--dependency-pin", "org.springframework:spring-core=6.2.12",
            "--dependency-deny", "com.acme:*",
        ])
        analysis = jm.ProjectAnalysis(dependencies=[
            jm.Dependency("com.acme", "internal", "1.0"),
            jm.Dependency("org.springframework", "spring-core", "5.3.1"),
            jm.Dependency("com.fasterxml.jackson.core", "jackson-core", "2.12.1"),
            jm.Dependency("org.apache.commons", "commons-lang3", "3.8"),
            jm.Dependency("org.apache.commons", "commons-lang3", "3.9"),
        ])
        recipes = "\n".join(jm.dependency_recipes(analysis, args))
        self.assertIn('artifactId: "spring-core"', recipes)
        self.assertIn('newVersion: "6.2.12"', recipes)
        self.assertIn('artifactId: "commons-lang3"', recipes)
        self.assertEqual(1, recipes.count('artifactId: "commons-lang3"'))
        self.assertNotIn("internal", recipes)
        self.assertNotIn("jackson-core", recipes)

    def test_policy_controls_profile_packs_dependencies_and_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps({
                "version": 1,
                "profile": "conservative",
                "targetJava": 17,
                "packs": {"testing": "none", "jakarta": "10"},
                "dependencies": {
                    "strategy": "latest",
                    "deny": ["com.acme:*"],
                    "pin": {"org.example:*": "2.0.0"},
                },
                "excludePaths": ["**/snapshots/**"],
                "verification": {
                    "build": "compile", "postChecks": "all", "strict": True,
                    "commands": ["./smoke-test"],
                },
                "reporting": {"format": "json"},
            }))
            args = jm.parse_args(["example", "--policy", str(path)])
            self.assertEqual(17, args.target_java)
            self.assertEqual("none", args.testing_modernization)
            self.assertEqual("10", args.jakarta)
            self.assertEqual("latest", args.dependency_strategy)
            self.assertEqual("2.0.0", args.dependency_pin["org.example:*"])
            self.assertIn("**/snapshots/**", args.exclusions)
            self.assertEqual("compile", args.verify)
            self.assertEqual("all", args.post_checks)
            self.assertTrue(args.strict_post_checks)
            self.assertEqual(["./smoke-test"], args.verify_command)
            self.assertEqual("json", args.report_format)

    def test_report_only_profile_has_no_rewrite_phases(self):
        args = jm.parse_args(["example", "--profile", "report-only"])
        phases = jm.migration_phases(
            jm.BuildRoot(Path("example"), "maven"), jm.ProjectAnalysis(), args,
        )
        self.assertEqual([], phases)
        self.assertEqual("none", args.dependency_strategy)
        self.assertEqual([], jm.artifacts(args))

    def test_local_input_is_copied_then_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "pom.xml").write_text("<project/>")
            output = root / "artifacts"
            workspace = root / "state"
            output.mkdir()
            workspace.mkdir()
            args = argparse.Namespace(
                output=output, workspace=workspace, force=False,
                shallow=True, timeout=10,
            )
            log = workspace / "log"
            copied, cloned = jm.prepare_repo(
                jm.RepoSpec(str(source)), args, os.environ.copy(), log, root / "askpass",
            )
            self.assertFalse(cloned)
            self.assertTrue((copied / "pom.xml").is_file())
            with self.assertRaises(jm.SkipMigration):
                jm.prepare_repo(
                    jm.RepoSpec(str(source)), args, os.environ.copy(), log, root / "askpass",
                )

    def test_old_gradle_wrapper_uses_container_gradle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = root / "gradlew"
            wrapper.write_text("#!/bin/sh\n")
            properties = root / "gradle" / "wrapper"
            properties.mkdir(parents=True)
            (properties / "gradle-wrapper.properties").write_text(
                "distributionUrl=https\\://services.gradle.org/distributions/gradle-4.10.3-bin.zip\n"
            )
            self.assertEqual("gradle", jm.executable(jm.BuildRoot(root, "gradle")))

    def test_maven_central_is_the_default_repository(self):
        args = jm.parse_args(["example"])
        self.assertEqual("maven-central", args.recipe_repository)
        self.assertEqual("6.46.1", args.maven_plugin_version)
        self.assertEqual("7.39.0", args.gradle_plugin_version)
        self.assertEqual("3.42.1", args.migrate_java_version)
        self.assertIsNone(args.artifact_repository)
        self.assertEqual("both", args.report_format)

    def test_json_and_markdown_reports_are_generated(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            args = argparse.Namespace(workspace=workspace, report_format="both")
            project = jm.ProjectResult(
                path="/tmp/output/app",
                build_tool="maven",
                status="changed",
                changed=True,
                manual_review=["docs/JAVA8.md"],
                analysis=jm.ProjectAnalysis(
                    features=["junit"],
                    dependencies=[jm.Dependency("junit", "junit", "4.12", "test")],
                    findings=["JUnit migration requires review."],
                    excluded_paths=["src/generated"],
                    external_configuration=[".github/workflows/build.yml"],
                ),
                phases=[jm.PhaseResult(
                    "testing-migration", "changed", True,
                    ["org.openrewrite.java.testing.junit5.JUnit4to5Migration"],
                )],
                checks=[jm.CheckResult(
                    "jdk-internals", "passed", ["jdeps", "--jdk-internals"], 0,
                    "detailed diagnostic output",
                )],
            )
            result = jm.Result(
                source="https://github.com/acme/app.git",
                path="/tmp/output/app",
                status="changed",
                changed=True,
                duration_seconds=12.5,
                log="/tmp/logs/app.log",
                diff_stat="2 files changed",
                projects=[project],
            )
            outputs = jm.write_result_reports(result, args, "app-12345678")
            self.assertEqual(
                {workspace / "reports" / "app-12345678.json",
                 workspace / "reports" / "app-12345678.md"},
                set(outputs),
            )
            markdown = (workspace / "reports" / "app-12345678.md").read_text()
            self.assertIn("# Java migration report: app", markdown)
            self.assertIn("JUnit4to5Migration", markdown)
            self.assertIn("docs/JAVA8.md", markdown)
            self.assertNotIn("detailed diagnostic output", markdown)
            structured = json.loads((workspace / "reports" / "app-12345678.json").read_text())
            self.assertEqual("detailed diagnostic output", structured["projects"][0]["checks"][0]["output"])

            summary = {
                "generated_at": "2026-10-01T00:00:00+00:00",
                "target_java": 21,
                "profile": "standard",
                "recipe_repository": "maven-central",
                "recipe_artifacts": ["org.example:recipes:1.0"],
                "counts": {"changed": 1, "failed": 0},
            }
            summary_outputs = jm.write_summary_reports(summary, [result], args)
            self.assertEqual(2, len(summary_outputs))
            summary_markdown = (workspace / "reports" / "summary.md").read_text()
            self.assertIn("# Java migration summary", summary_markdown)
            self.assertIn("[app-", summary_markdown)

    def test_repository_modes_generate_isolated_gradle_repositories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            central = jm.parse_args(["example"])
            central_path = root / "central.gradle"
            jm.write_gradle_init(central_path, central, "example.Recipe", root / "rewrite.yml")
            central_text = central_path.read_text()
            self.assertIn("mavenCentral()", central_text)
            self.assertIn('activeRecipe("example.Recipe")', central_text)
            self.assertIn("configFile = file(", central_text)
            self.assertNotIn("codegenomeproject.org", central_text)
            self.assertNotIn("mavenLocal()", central_text)
            self.assertIn('exclusion("**/generated/**"', central_text)

            local = jm.parse_args(["example", "--recipe-repository", "maven-local"])
            local_path = root / "local.gradle"
            jm.write_gradle_init(local_path, local, "example.Recipe", root / "rewrite.yml")
            self.assertIn("mavenLocal()", local_path.read_text())

            codegenome = jm.parse_args(["example", "--recipe-repository", "codegenome"])
            codegenome_path = root / "codegenome.gradle"
            jm.write_gradle_init(
                codegenome_path, codegenome, "example.Recipe", root / "rewrite.yml"
            )
            codegenome_text = codegenome_path.read_text()
            self.assertIn(jm.CODE_GENOME_URL, codegenome_text)
            self.assertIn('System.getenv("CODE_GENOME_TOKEN")', codegenome_text)
            self.assertEqual("3.45.0", codegenome.migrate_java_version)

    def test_central_maven_settings_do_not_add_code_genome(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.xml"
            args = jm.parse_args(["example"])
            args.maven_settings = Path(directory) / "missing-settings.xml"
            jm.write_maven_settings(path, args, {})
            content = path.read_text()
            self.assertIn("<settings", content)
            self.assertNotIn("codegenome", content)

    def test_parent_git_boundary_is_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            (parent / ".git").mkdir()
            copied_project = parent / "artifacts" / "app"
            copied_project.mkdir(parents=True)
            marker = copied_project / ".git"
            with jm.isolate_from_parent_git(copied_project):
                self.assertTrue(marker.is_file())
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
