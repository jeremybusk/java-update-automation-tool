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
    STAGES, Portfolio, PortfolioError, Repository, artifact_path, cohort_complete,
    migration_waves, now, read_json,
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
        "openrewrite": {key: rewrite[key] for key in ("artifact_repository", "repository_username_env",
                        "repository_token_env", "source_cache", "source_lock") if key in rewrite},
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


def _migration_attempt(repo: Repository, config: dict[str, Any], state: Path, *,
                       execute: bool, blocked_by: list[str]) -> dict[str, Any]:
    """Prepare or execute one attempt; the stage retains its result."""
    import java_migrator as engine

    plan_path = artifact_path(state, STAGES[2], "repositories", repo.key)
    result_path = artifact_path(state, STAGES[3], "repositories", repo.key)
    directory = result_path.parent / "attempts" / uuid.uuid4().hex[:8]
    directory.mkdir(parents=True)
    policy_path = directory / "migration-policy.json"
    write_json(policy_path, _migration_policy(config))
    rewrite = config.get("migration", {}).get("openrewrite", {})
    command = [
        sys.executable, str(Path(__file__).resolve().parents[1] / "migrate.py"), repo.source,
        "--policy", str(policy_path),
        "--recipe-repository", rewrite.get("recipe_repository", "maven-central"),
        "--max-depth", str(config.get("discovery", {}).get("max_depth", 5)),
        "--output", str(directory / "worktree"), "--workspace", str(directory / "engine"),
        "--commit",
    ]
    result: dict[str, Any] = {
        "schema_version": 1, "artifact_type": "repository-migration-result",
        "stage": STAGES[3], "generated_at": now(), "repository": repo.key,
        "status": "planned", "executed": False, "command": command,
        "inputs": {"plan": str(plan_path), "policy": str(policy_path)},
        "output": str(directory / "worktree" / engine.destination_name(repo.source)),
    }
    if not execute:
        return result
    if blocked_by:
        result.update(status="blocked", blocked_by=blocked_by)
        return result
    try:
        completed = execute_migration(command)
        result.update(executed=True, exit_code=completed.returncode, status="failed")
        if completed.returncode == 0:
            summary = read_json(directory / "engine" / "reports" / "summary.json")
            engine_results = summary.get("results", [])
            migrated = bool(engine_results) and all(
                item["status"] in {"changed", "unchanged"} for item in engine_results)
            result["status"] = "migrated" if migrated else "analyzed"
    except (OSError, PortfolioError) as exc:
        result.update(status="failed", error=str(exc))
    return result


def migration_stage(selected: Sequence[Repository], portfolio: Portfolio, config: dict[str, Any], state: Path,
                    execute: bool, *, reuse_completed: bool = True) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    waves, ordering_issues = migration_waves(selected)
    if execute and ordering_issues:
        raise PortfolioError("cannot execute migration:\n- " + "\n- ".join(ordering_issues))
    by_key = {repo.key: repo for repo in selected}
    aliases = {alias: repo.key for repo in selected for alias in (repo.key, repo.repo_name)}
    successful: set[str] = set()
    # Check all required plans before starting any migration.
    for repo in selected:
        read_json(artifact_path(state, STAGES[2], "repositories", repo.key))
    for key in (key for wave in waves for key in wave):
        repo = by_key[key]
        result_path = artifact_path(state, STAGES[3], "repositories", repo.key)
        if reuse_completed and result_path.exists():
            existing = read_json(result_path)
            if existing["status"] == "migrated":
                successful.add(repo.key)
                outputs.append(existing)
                continue
        blocked_by = sorted({aliases[dep] for dep in repo.depends_on
                             if dep in aliases and aliases[dep] not in successful})
        result = _migration_attempt(repo, config, state, execute=execute, blocked_by=blocked_by)
        if result["status"] == "migrated":
            successful.add(repo.key)
        write_json(result_path, result)
        outputs.append(result)
    repository_results = {item["repository"]: item for item in outputs}
    for kind, field in (("applications", "application_id"),
                        ("application-groups", "application_group_id")):
        for key in sorted({getattr(repo, field) for repo in selected}):
            members = [repo for repo in selected if getattr(repo, field) == key]
            results = [repository_results[repo.key] for repo in members]
            complete_cohort = cohort_complete(selected, portfolio.repositories, field, key)
            statuses = {item["status"] for item in results}
            if "failed" in statuses:
                status = "failed"
            elif "blocked" in statuses:
                status = "blocked"
            elif not complete_cohort:
                status = "incomplete-cohort"
            elif results and statuses <= {"complete", "migrated"}:
                status = "complete"
            else:
                status = "planned"
            summary = {
                "schema_version": 1, "artifact_type": f"{kind[:-1]}-migration-result",
                "stage": STAGES[3], "generated_at": now(), "id": key,
                "cohort_complete": complete_cohort,
                "status": status,
                "results": [{"repository": item["repository"], "status": item["status"],
                             "executed": item["executed"]} for item in results],
            }
            write_json(artifact_path(state, STAGES[3], kind, key), summary)
            outputs.append(summary)
    return outputs


def main(argv: Sequence[str] | None = None) -> int:
    from .workflow import main as workflow_main
    return workflow_main(argv)
