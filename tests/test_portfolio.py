import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from java_update_tool import core
from java_update_tool.cli import main
from java_update_tool.runs import git, run_directory
import java_migrator as engine


CONFIG = """\
schema_version: 1
targets:
  java:
    desired: 21
    acceptable: [17, 21]
  spring_boot:
    desired: '3.5.1'
    acceptable: ['3.4.x', '3.5.x']
alignment:
  dependencies:
    include: ['org.example:*']
    pins:
      'org.example:shared': '2.0.0'
migration:
  profile: report-only
  openrewrite:
    recipe_repository: maven-central
    artifacts: ['org.openrewrite.recipe:rewrite-spring:6.40.0']
workflow:
  mode: unattended
"""


def make_maven(path: Path, java: str, dependency: str, boot: str = "3.4.2") -> None:
    path.mkdir(parents=True)
    (path / "pom.xml").write_text(f"""\
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>{boot}</version>
  </parent>
  <groupId>example</groupId><artifactId>app</artifactId><version>1</version>
  <properties><java.version>{java}</java.version></properties>
  <dependencies><dependency><groupId>org.example</groupId><artifactId>shared</artifactId>
    <version>{dependency}</version></dependency></dependencies>
</project>
""", encoding="utf-8")


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        make_maven(self.root / "api", "17", "1.0.0")
        make_maven(self.root / "worker", "21", "2.0.0")
        (self.root / "config.yml").write_text(CONFIG, encoding="utf-8")
        (self.root / "repos.yml").write_text("""\
schema_version: 1
repositories:
  - repo_name: api
    repo_id: api-id
    source: api
    application_id: orders
    application_group_id: commerce
  - repo_name: worker
    source: worker
    application_id: orders
    application_group_id: commerce
    depends_on: [api-id]
""", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_validation_and_optional_repo_id(self):
        portfolio = core.load_portfolio(self.root / "repos.yml")
        self.assertEqual(["api-id", "worker"], [repo.key for repo in portfolio.repositories])
        self.assertEqual([], core.validate_targets(core.load_config(self.root / "config.yml")))

    def test_selection_marks_partial_cohort_incomplete(self):
        portfolio = core.load_portfolio(self.root / "repos.yml")
        selected = core.select_repositories(portfolio, repos=["api-id"], applications=[], groups=[],
                                            all_repositories=False)
        self.assertEqual(["api-id"], [repo.key for repo in selected])
        self.assertFalse(core.cohort_complete(selected, portfolio.repositories, "application_id", "orders"))

    def test_end_to_end_plan_and_markdown(self):
        state = self.root / "state"
        status = main([
            "--config", str(self.root / "config.yml"), "--portfolio", str(self.root / "repos.yml"),
            "--state", str(state), "run", "--application", "orders",
        ])
        self.assertEqual(0, status)
        state = self._run_root()
        discovery = json.loads((state / "01-discovery/repositories/api-id/result.json").read_text())
        self.assertEqual(["17"], discovery["summary"]["java_versions"])
        self.assertEqual(["3.4.2"], discovery["summary"]["spring_boot_versions"])
        assessment = json.loads((state / "02-assessment/applications/orders/result.json").read_text())
        self.assertTrue(assessment["cohort_complete"])
        self.assertEqual("attention-required", assessment["status"])
        self.assertEqual("org.example:shared", assessment["alignment"]["dependencies"][0]["coordinate"])
        self.assertEqual("2.0.0", assessment["alignment"]["dependencies"][0]["target"])
        plan = json.loads((state / "03-planning/applications/orders/plan.json").read_text())
        self.assertEqual([["api-id"], ["worker"]], plan["migration_waves"])
        markdown = (state / "reports/03-planning/applications/orders/plan.md").read_text()
        self.assertIn("- [ ]", markdown)
        self.assertIn("JSON is the source of truth", markdown)

    def test_assessment_can_rerun_from_discovery(self):
        state = self.root / "state"
        common = ["--config", str(self.root / "config.yml"), "--portfolio", str(self.root / "repos.yml"),
                  "--state", str(state)]
        self.assertEqual(0, main(common + ["run", "--application", "orders", "--through", "01-discovery"]))
        self.assertEqual(0, main(common + ["run", "--resume", "latest", "--application", "orders", "--from", "02-assessment",
                                           "--through", "03-planning"]))
        self.assertTrue((self._run_root() / "03-planning/repositories/worker/plan.json").is_file())

    def test_invalid_cross_group_application_is_rejected(self):
        content = (self.root / "repos.yml").read_text().replace(
            "application_group_id: commerce\n    depends_on", "application_group_id: finance\n    depends_on")
        path = self.root / "bad.yml"
        path.write_text(content)
        with self.assertRaises(core.PortfolioError):
            core.load_portfolio(path)

    def _run_root(self):
        return run_directory(self.root / "state")

    def _run(self, *arguments):
        if "--from" in arguments:
            arguments = ("--resume", "latest", *arguments)
        return main([
            "--config", str(self.root / "config.yml"), "--portfolio", str(self.root / "repos.yml"),
            "--state", str(self.root / "state"), "run", *arguments,
        ])

    def _migration_result(self, key, kind="repositories"):
        return core.read_json(core.artifact_path(self._run_root(), "04-migration", kind, key))

    def _change_repositories(self, change):
        path = self.root / "repos.yml"
        data = yaml.safe_load(path.read_text())
        change(data["repositories"])
        path.write_text(yaml.safe_dump(data), encoding="utf-8")

    def _fake_migration(self, command):
        source = Path(command[2])
        output = Path(command[command.index("--output") + 1]) / source.name
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, output)
        pom = output / "pom.xml"
        pom.write_text(pom.read_text().replace(">17<", ">21<").replace("3.4.2", "3.5.1").replace("1.0.0", "2.0.0"))
        git(output, "add", "--all")
        git(output, "commit", "-m", "Migrate build")
        workspace = Path(command[command.index("--workspace") + 1])
        core.write_json(workspace / "reports/summary.json", {"results": [{"status": "changed"}]})
        return subprocess.CompletedProcess(command, 0)

    def _execute_stage_four(self):
        return self._run("--from", "04-migration", "--through", "04-migration", "--execute")

    def test_stage_four_preparation_does_not_execute_and_passes_pins(self):
        self.assertEqual(0, self._run())
        with patch("java_update_tool.cli.execute_migration") as run:
            self.assertEqual(0, self._run("--from", "04-migration", "--through", "04-migration"))
        run.assert_not_called()
        for key in ("api-id", "worker"):
            result = self._migration_result(key)
            self.assertEqual("planned", result["status"])
            self.assertFalse(result["executed"])
            self.assertIn("--commit", result["command"])
            self.assertNotIn("--force", result["command"])
            self.assertIn("attempts", Path(result["inputs"]["policy"]).parts)
            policy = core.read_json(Path(result["inputs"]["policy"]))
            self.assertEqual({"org.example:shared": "2.0.0"}, policy["dependencies"]["pin"])

    def test_stage_four_pins_generate_exact_engine_recipes(self):
        path = self.root / "config.yml"
        for profile in ("standard", "conservative"):
            with self.subTest(profile=profile):
                path.write_text(CONFIG.replace("profile: report-only", f"profile: {profile}"), encoding="utf-8")
                self.assertEqual(0, self._run("--through", "04-migration"))
                result = self._migration_result("api-id")
                args = engine.parse_args(["example", "--policy", result["inputs"]["policy"]])
                analysis = engine.ProjectAnalysis(dependencies=[
                    engine.Dependency("org.example", "shared", "1.0.0"),
                    engine.Dependency("org.example", "unpinned", "1.0.0"),
                ])
                recipes = "\n".join(engine.dependency_recipes(analysis, args))
                self.assertIn('artifactId: "shared"', recipes)
                self.assertIn('newVersion: "2.0.0"', recipes)
                if profile == "conservative":
                    self.assertNotIn('artifactId: "unpinned"', recipes)

    def test_stage_four_executes_in_dependency_order_with_name_alias(self):
        def reverse_with_alias(repositories):
            repositories[1]["depends_on"] = ["api"]
            repositories.reverse()
        self._change_repositories(reverse_with_alias)
        self.assertEqual(0, self._run())
        with patch("java_update_tool.cli.execute_migration",
                   side_effect=self._fake_migration) as run:
            self.assertEqual(0, self._execute_stage_four())
        self.assertEqual(["api-id", "worker"], [Path(call.args[0][2]).name for call in run.call_args_list])
        for key in ("api-id", "worker"):
            result = self._migration_result(key)
            self.assertEqual("migrated", result["status"])
            self.assertTrue(result["executed"])
        self.assertEqual("complete", self._migration_result("orders", "applications")["status"])

    def test_stage_four_failure_blocks_transitive_dependents_but_runs_independent_repos(self):
        def add_repositories(repositories):
            repositories[1]["application_id"] = "jobs"
            repositories.extend([
                {"repo_name": "consumer", "source": "worker", "application_id": "jobs",
                 "application_group_id": "commerce", "depends_on": ["worker"]},
                {"repo_name": "independent", "source": "api", "application_id": "orders",
                 "application_group_id": "commerce"},
            ])
            repositories.reverse()
        self._change_repositories(add_repositories)
        self.assertEqual(0, self._run())
        def fail_api(command):
            if Path(command[2]).name == "api-id":
                return subprocess.CompletedProcess(command, 7)
            return self._fake_migration(command)
        with patch("java_update_tool.cli.execute_migration", side_effect=fail_api) as run:
            self.assertEqual(1, self._execute_stage_four())
        self.assertEqual(2, run.call_count)
        api = self._migration_result("api-id")
        self.assertEqual("failed", api["status"])
        self.assertEqual(7, api["exit_code"])
        for key, dependency in (("worker", "api-id"), ("consumer", "worker")):
            result = self._migration_result(key)
            self.assertEqual("blocked", result["status"])
            self.assertFalse(result["executed"])
            self.assertEqual([dependency], result["blocked_by"])
        self.assertEqual("migrated", self._migration_result("independent")["status"])
        self.assertEqual("blocked", self._migration_result("jobs", "applications")["status"])
        self.assertEqual("failed", self._migration_result("commerce", "application-groups")["status"])
        state = self._run_root()
        failure_report = (state / "reports/04-migration/repositories/api-id/result.md").read_text()
        self.assertIn("Engine exit code: `7`", failure_report)
        blocked_report = (state / "reports/04-migration/repositories/worker/result.md").read_text()
        self.assertIn("Blocked by: `api-id`", blocked_report)
        self.assertEqual(1, core.read_json(state / "run.json")["exit_code"])

    def test_stage_four_partial_cohort_failure_returns_nonzero(self):
        self.assertEqual(0, self._run("--repo", "api-id"))
        with patch("java_update_tool.cli.execute_migration", return_value=subprocess.CompletedProcess([], 1)):
            self.assertEqual(1, self._run("--repo", "api-id", "--from", "04-migration",
                                         "--through", "04-migration", "--execute"))
        summary = self._migration_result("orders", "applications")
        self.assertFalse(summary["cohort_complete"])
        self.assertEqual("failed", summary["status"])

    def test_stage_four_unselected_dependency_requires_validation_evidence(self):
        self.assertEqual(0, self._run("--repo", "worker"))
        with patch("java_update_tool.cli.execute_migration") as run:
            self.assertEqual(2, self._run("--repo", "worker", "--from", "04-migration",
                                         "--through", "04-migration", "--execute"))
        run.assert_not_called()

    def test_stage_four_cycle_is_rejected_before_execution(self):
        self._change_repositories(lambda repositories: repositories[0].update(depends_on=["worker"]))
        self.assertEqual(0, self._run())
        with patch("java_update_tool.cli.execute_migration") as run:
            self.assertEqual(2, self._execute_stage_four())
        run.assert_not_called()

    def test_stage_four_missing_plan_is_rejected_before_execution(self):
        self.assertEqual(0, self._run())
        core.artifact_path(self._run_root(), "03-planning", "repositories", "worker").unlink()
        with patch("java_update_tool.cli.execute_migration") as run:
            self.assertEqual(2, self._execute_stage_four())
        run.assert_not_called()

    def test_stage_four_launch_failure_is_recorded_and_blocks_dependents(self):
        self.assertEqual(0, self._run())
        with patch("java_update_tool.cli.execute_migration", side_effect=OSError("cannot launch engine")) as run:
            self.assertEqual(1, self._execute_stage_four())
        run.assert_called_once()
        result = self._migration_result("api-id")
        self.assertEqual("failed", result["status"])
        self.assertFalse(result["executed"])
        self.assertEqual("cannot launch engine", result["error"])
        self.assertEqual("blocked", self._migration_result("worker")["status"])

    def test_removed_options_are_rejected_before_creating_state(self):
        entry = Path(__file__).resolve().parents[1] / "portfolio.py"
        for arguments in (["--legacy", "validate"], ["run", "--refresh"], ["run", "--no-refresh"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, "-B", str(entry), "--state", str(self.root / "state"),
                                         *arguments], capture_output=True, text=True)
                self.assertEqual(2, result.returncode, result.stderr)
                self.assertIn("unrecognized arguments", result.stderr)
                self.assertFalse((self.root / "state").exists())

    def test_report_script_renders_retained_runs_by_latest_or_explicit_id(self):
        self.assertEqual(0, self._run())
        root = self._run_root()
        script = Path(__file__).resolve().parents[1] / "scripts/render_reports.py"
        markdown = root / "reports/03-planning/repositories/api-id/plan.md"
        for run_id in ("latest", root.name):
            with self.subTest(run_id=run_id):
                markdown.unlink()
                result = subprocess.run([sys.executable, "-B", str(script), "--state", str(self.root / "state"),
                                         "--run-id", run_id], capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertTrue(markdown.is_file())
                self.assertIn(str(root / "reports"), result.stdout)

    def test_report_script_rejects_flat_state_without_modifying_artifacts(self):
        state = self.root / "flat-state"
        artifact = core.artifact_path(state, "01-discovery", "repositories", "api-id")
        core.write_json(artifact, {"schema_version": 1, "stage": "01-discovery", "status": "complete"})
        original = artifact.read_bytes()
        script = Path(__file__).resolve().parents[1] / "scripts/render_reports.py"
        result = subprocess.run([sys.executable, "-B", str(script), "--state", str(state)],
                                capture_output=True, text=True)
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn("error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((state / "reports").exists())
        self.assertEqual(original, artifact.read_bytes())


if __name__ == "__main__":
    unittest.main()
