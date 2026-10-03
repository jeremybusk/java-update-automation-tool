import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from java_update_tool import core
from java_update_tool.cli import legacy_main as main
import java_migrator as legacy


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
        self.assertEqual(0, main(common + ["run", "--application", "orders", "--from", "02-assessment",
                                           "--through", "03-planning"]))
        self.assertTrue((state / "03-planning/repositories/worker/plan.json").is_file())

    def test_invalid_cross_group_application_is_rejected(self):
        content = (self.root / "repos.yml").read_text().replace(
            "application_group_id: commerce\n    depends_on", "application_group_id: finance\n    depends_on")
        path = self.root / "bad.yml"
        path.write_text(content)
        with self.assertRaises(core.PortfolioError):
            core.load_portfolio(path)

    def _run(self, *arguments):
        return main([
            "--config", str(self.root / "config.yml"), "--portfolio", str(self.root / "repos.yml"),
            "--state", str(self.root / "state"), "run", *arguments,
        ])

    def _migration_result(self, key, kind="repositories"):
        return core.read_json(core.artifact_path(self.root / "state", "04-migration", kind, key))

    def _change_repositories(self, change):
        path = self.root / "repos.yml"
        data = yaml.safe_load(path.read_text())
        change(data["repositories"])
        path.write_text(yaml.safe_dump(data), encoding="utf-8")

    def _execute_stage_four(self):
        return self._run("--from", "04-migration", "--through", "04-migration", "--execute")

    def test_stage_four_preparation_does_not_execute_and_passes_pins(self):
        self.assertEqual(0, self._run())
        with patch("java_update_tool.cli.subprocess.run") as run:
            self.assertEqual(0, self._run("--from", "04-migration", "--through", "04-migration"))
        run.assert_not_called()
        for key in ("api-id", "worker"):
            result = self._migration_result(key)
            self.assertEqual("planned", result["status"])
            self.assertFalse(result["executed"])
            policy = core.read_json(Path(result["inputs"]["policy"]))
            self.assertEqual({"org.example:shared": "2.0.0"}, policy["dependencies"]["pin"])

    def test_stage_four_pins_generate_exact_engine_recipes(self):
        path = self.root / "config.yml"
        for profile in ("standard", "conservative"):
            with self.subTest(profile=profile):
                path.write_text(CONFIG.replace("profile: report-only", f"profile: {profile}"), encoding="utf-8")
                self.assertEqual(0, self._run("--through", "04-migration"))
                result = self._migration_result("api-id")
                args = legacy.parse_args(["example", "--policy", result["inputs"]["policy"]])
                analysis = legacy.ProjectAnalysis(dependencies=[
                    legacy.Dependency("org.example", "shared", "1.0.0"),
                    legacy.Dependency("org.example", "unpinned", "1.0.0"),
                ])
                recipes = "\n".join(legacy.dependency_recipes(analysis, args))
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
        with patch("java_update_tool.cli.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(0, self._execute_stage_four())
        self.assertEqual(["api", "worker"], [Path(call.args[0][2]).name for call in run.call_args_list])
        for key in ("api-id", "worker"):
            result = self._migration_result(key)
            self.assertEqual("complete", result["status"])
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
        with patch("java_update_tool.cli.subprocess.run", side_effect=[
            subprocess.CompletedProcess([], 7), subprocess.CompletedProcess([], 0),
        ]) as run:
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
        self.assertEqual("complete", self._migration_result("independent")["status"])
        self.assertEqual("blocked", self._migration_result("jobs", "applications")["status"])
        self.assertEqual("failed", self._migration_result("commerce", "application-groups")["status"])
        state = self.root / "state"
        failure_report = (state / "reports/04-migration/repositories/api-id/result.md").read_text()
        self.assertIn("Engine exit code: `7`", failure_report)
        blocked_report = (state / "reports/04-migration/repositories/worker/result.md").read_text()
        self.assertIn("Blocked by: `api-id`", blocked_report)
        receipts = [core.read_json(path) for path in (state / "runs").glob("*.json")]
        self.assertTrue(any(record.get("exit_code") == 1 for record in receipts))

    def test_stage_four_partial_cohort_failure_returns_nonzero(self):
        self.assertEqual(0, self._run("--repo", "api-id"))
        with patch("java_update_tool.cli.subprocess.run", return_value=subprocess.CompletedProcess([], 1)):
            self.assertEqual(1, self._run("--repo", "api-id", "--from", "04-migration",
                                         "--through", "04-migration", "--execute"))
        summary = self._migration_result("orders", "applications")
        self.assertFalse(summary["cohort_complete"])
        self.assertEqual("failed", summary["status"])

    def test_stage_four_single_repo_does_not_require_unselected_dependency(self):
        self.assertEqual(0, self._run("--repo", "worker"))
        with patch("java_update_tool.cli.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(0, self._run("--repo", "worker", "--from", "04-migration",
                                         "--through", "04-migration", "--execute"))
        run.assert_called_once()
        self.assertEqual("complete", self._migration_result("worker")["status"])
        self.assertEqual("incomplete-cohort", self._migration_result("orders", "applications")["status"])

    def test_stage_four_cycle_is_rejected_before_execution(self):
        self._change_repositories(lambda repositories: repositories[0].update(depends_on=["worker"]))
        self.assertEqual(0, self._run())
        with patch("java_update_tool.cli.subprocess.run") as run:
            self.assertEqual(2, self._execute_stage_four())
        run.assert_not_called()

    def test_stage_four_missing_plan_is_rejected_before_execution(self):
        self.assertEqual(0, self._run())
        core.artifact_path(self.root / "state", "03-planning", "repositories", "worker").unlink()
        with patch("java_update_tool.cli.subprocess.run") as run:
            self.assertEqual(2, self._execute_stage_four())
        run.assert_not_called()

    def test_stage_four_launch_failure_is_recorded_and_blocks_dependents(self):
        self.assertEqual(0, self._run())
        with patch("java_update_tool.cli.subprocess.run", side_effect=OSError("cannot launch engine")) as run:
            self.assertEqual(1, self._execute_stage_four())
        run.assert_called_once()
        result = self._migration_result("api-id")
        self.assertEqual("failed", result["status"])
        self.assertFalse(result["executed"])
        self.assertEqual("cannot launch engine", result["error"])
        self.assertEqual("blocked", self._migration_result("worker")["status"])


if __name__ == "__main__":
    unittest.main()
