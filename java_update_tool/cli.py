"""Public CLI and migration execution for the six-stage portfolio workflow."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Sequence

from .core import (
    STAGES, PortfolioError, artifact_path, migration_waves, now, read_json,
    select_repositories, write_json,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Investigate, plan, and migrate related Java Git repositories.")
    result.add_argument("--config", type=Path, default=Path("java-update.yml"))
    result.add_argument("--portfolio", type=Path, default=Path("repositories.yml"))
    result.add_argument("--state", type=Path, default=Path(".java-update"))
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="validate configuration and repository associations")
    run = commands.add_parser("run", help="run one or more ordered stages")
    _selectors(run)
    run.add_argument("--from", dest="from_stage", choices=STAGES)
    run.add_argument("--through", choices=STAGES, default=STAGES[2])
    run.add_argument("--execute", action="store_true",
                     help="execute stage 04 with the existing OpenRewrite migration engine")
    report = commands.add_parser("report", help="regenerate Markdown solely from JSON artifacts")
    _selectors(report)
    run.add_argument("--resume", metavar="RUN_ID", help="resume a retained run; use latest for the most recent")
    run.add_argument("--mode", choices=("manual", "unattended"))
    run.add_argument("--show-diffs", action=argparse.BooleanOptionalAction, default=None)
    run.add_argument("--include-dependencies", action=argparse.BooleanOptionalAction, default=None)
    run.add_argument("--dependency-override", action=argparse.BooleanOptionalAction, default=None)
    run.add_argument("--enable-publishing", action=argparse.BooleanOptionalAction, default=None)
    run.add_argument("--publish-target", action="append", choices=("local_repo", "src_repo", "dst_repo"))
    run.add_argument("--source-selection", choices=("default", "current"))
    run.add_argument("--draft-request", action=argparse.BooleanOptionalAction, default=None)
    report.add_argument("--run-id", default="latest")
    approval = commands.add_parser("approve", help="approve an unchanged completed stage")
    approval.add_argument("run_id")
    approval.add_argument("--stage", choices=STAGES, required=True)
    commands.add_parser("runs", help="list retained runs")
    diff = commands.add_parser("diff", help="compare source, output, and policy between runs")
    diff.add_argument("run_id")
    diff.add_argument("--compare-to", required=True)
    export = commands.add_parser("export-evidence", help="export redacted, portable inspection evidence")
    export.add_argument("run_id")
    export.add_argument("--output", type=Path, required=True)
    cleanup = commands.add_parser("prune", help="inspect retained-run cleanup; dry-run unless --apply")
    cleanup.add_argument("--apply", action="store_true")
    return result


def _selectors(target: argparse.ArgumentParser) -> None:
    target.add_argument("--repo", action="append", default=[], help="repo_id or repo_name; repeatable")
    target.add_argument("--application", action="append", default=[], help="application_id; repeatable")
    target.add_argument("--application-group", action="append", default=[], help="application_group_id; repeatable")
    target.add_argument("--all", action="store_true", dest="all_repositories")


def _selected(args: argparse.Namespace, portfolio: Any) -> list[Any]:
    return select_repositories(portfolio, repos=args.repo, applications=args.application,
                               groups=args.application_group, all_repositories=args.all_repositories)


def _migration_policy(config: dict[str, Any]) -> dict[str, Any]:
    migration = config.get("migration", {})
    rewrite = migration.get("openrewrite", {})
    return {
        "version": 1, "profile": migration.get("profile", "standard"),
        "targetJava": int(config["targets"]["java"]["desired"]),
        "buildTool": config.get("discovery", {}).get("build_tool", "auto"),
        "recipes": rewrite.get("recipes", []), "artifacts": rewrite.get("artifacts", []),
        "dependencies": {"pin": {
            pattern: str(version)
            for pattern, version in config.get("alignment", {}).get("dependencies", {}).get("pins", {}).items()
        }},
        "verification": migration.get("verification", {"build": "test", "postChecks": "jdk", "strict": False}),
        "reporting": {"format": "both"},
    }


def execute_migration(command: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({"GIT_AUTHOR_NAME": "Java Update Automation", "GIT_AUTHOR_EMAIL": "java-update@localhost",
                "GIT_COMMITTER_NAME": "Java Update Automation", "GIT_COMMITTER_EMAIL": "java-update@localhost"})
    from .operations import CONTEXT, execute
    context = CONTEXT.get()
    if context is None:
        raise PortfolioError("migration execution requires a retained run context")
    env["JAVA_UPDATE_RUN_ROOT"] = str(context["root"])
    output = execute(command, Path(__file__).resolve().parents[1], env, 3600)
    return subprocess.CompletedProcess(command, 0, output)


def migration_stage(selected: Sequence[Any], portfolio: Any, config: dict[str, Any], state: Path,
                    execute: bool) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    policy = _migration_policy(config)
    recipe_repository = config.get("migration", {}).get("openrewrite", {}).get("recipe_repository", "maven-central")
    waves, ordering_issues = migration_waves(selected)
    if execute and ordering_issues:
        raise PortfolioError("cannot execute migration:\n- " + "\n- ".join(ordering_issues))
    by_key = {repo.key: repo for repo in selected}
    aliases = {alias: repo.key for repo in selected for alias in (repo.key, repo.repo_name)}
    statuses: dict[str, str] = {}
    # Check all required plans before starting any migration.
    for repo in selected:
        read_json(artifact_path(state, STAGES[2], "repositories", repo.key))
    for key in (key for wave in waves for key in wave):
        repo = by_key[key]
        plan_path = artifact_path(state, STAGES[2], "repositories", repo.key)
        result_path = artifact_path(state, STAGES[3], "repositories", repo.key)
        if result_path.exists():
            existing = read_json(result_path)
            if existing["status"] == "migrated":
                statuses[repo.key] = "complete"
                outputs.append(existing)
                continue
        directory = result_path.parent / "attempts" / uuid.uuid4().hex[:8]
        directory.mkdir(parents=True)
        policy_path = directory / "migration-policy.json"
        write_json(policy_path, policy)
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / "migrate.py"), repo.source,
                   "--policy", str(policy_path), "--recipe-repository", recipe_repository,
                   "--output", str(directory / "worktree"), "--workspace", str(directory / "engine"),
                   "--commit"]
        result: dict[str, Any] = {
            "schema_version": 1, "artifact_type": "repository-migration-result",
            "stage": "04-migration", "generated_at": now(), "repository": repo.key,
            "status": "planned", "executed": False, "command": command,
            "inputs": {"plan": str(plan_path), "policy": str(policy_path)},
        }
        if execute:
            blocked_by = sorted({aliases[dep] for dep in repo.depends_on
                                 if dep in aliases and statuses.get(aliases[dep]) != "complete"})
            if blocked_by:
                result.update({"status": "blocked", "blocked_by": blocked_by})
            else:
                try:
                    completed = execute_migration(command)
                    result.update({"executed": True, "exit_code": completed.returncode,
                                   "status": "complete" if completed.returncode == 0 else "failed"})
                except (OSError, PortfolioError) as exc:
                    result.update({"status": "failed", "error": str(exc)})
        import java_migrator as engine
        result["output"] = str(directory / "worktree" / engine.destination_name(repo.source))
        if result["status"] == "complete":
            try:
                summary = read_json(directory / "engine" / "reports" / "summary.json")
                engine_results = summary.get("results", [])
                if engine_results and all(item["status"] in {"changed", "unchanged"} for item in engine_results):
                    result["status"] = "migrated"
                else:
                    result["status"] = "analyzed"
            except PortfolioError as exc:
                result.update({"status": "failed", "error": str(exc)})
        statuses[repo.key] = "complete" if result["status"] == "migrated" else result["status"]
        write_json(artifact_path(state, STAGES[3], "repositories", repo.key), result)
        outputs.append(result)
    for kind, field in (("applications", "application_id"),
                        ("application-groups", "application_group_id")):
        for key in sorted({getattr(repo, field) for repo in selected}):
            members = [repo for repo in selected if getattr(repo, field) == key]
            results = [item for item in outputs if item.get("repository") in {repo.key for repo in members}]
            complete_cohort = {
                repo.key for repo in members
            } == {repo.key for repo in portfolio.repositories if getattr(repo, field) == key}
            summary = {
                "schema_version": 1, "artifact_type": f"{kind[:-1]}-migration-result",
                "stage": "04-migration", "generated_at": now(), "id": key,
                "cohort_complete": complete_cohort,
                "status": ("failed" if any(item["status"] == "failed" for item in results) else
                           "blocked" if any(item["status"] == "blocked" for item in results) else
                           "incomplete-cohort" if not complete_cohort else
                           "complete" if results and all(item["status"] in {"complete", "migrated"} for item in results)
                           else "planned"),
                "results": [{"repository": item["repository"], "status": item["status"],
                             "executed": item["executed"]} for item in results],
            }
            write_json(artifact_path(state, STAGES[3], kind, key), summary)
            outputs.append(summary)
    return outputs


def main(argv: Sequence[str] | None = None) -> int:
    from .workflow import main as workflow_main
    return workflow_main(argv)
