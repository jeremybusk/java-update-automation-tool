import json
import tempfile
import unittest
from pathlib import Path

from java_update_tool import core
from java_update_tool.cli import main


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


if __name__ == "__main__":
    unittest.main()
