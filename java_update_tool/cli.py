"""Command-line orchestration for the four-stage portfolio workflow."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from .core import (
    STAGES, PortfolioError, artifact_path, assess, discover_repository, load_config,
    load_portfolio, now, plan, read_json, render_reports, select_repositories,
    summarize_discoveries, validate_targets, write_json,
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
    run.add_argument("--from", dest="from_stage", choices=STAGES, default=STAGES[0])
    run.add_argument("--through", choices=STAGES, default=STAGES[2])
    run.add_argument("--refresh", action=argparse.BooleanOptionalAction, default=True,
                     help="fetch remote discovery checkouts before inspection")
    run.add_argument("--execute", action="store_true",
                     help="execute stage 04 with the existing OpenRewrite migration engine")
    report = commands.add_parser("report", help="regenerate Markdown solely from JSON artifacts")
    _selectors(report)
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
        "verification": migration.get("verification", {"build": "test", "postChecks": "jdk", "strict": False}),
        "reporting": {"format": "both"},
    }


def migration_stage(selected: Sequence[Any], portfolio: Any, config: dict[str, Any], state: Path,
                    execute: bool) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    policy = _migration_policy(config)
    recipe_repository = config.get("migration", {}).get("openrewrite", {}).get("recipe_repository", "maven-central")
    for repo in selected:
        plan_path = artifact_path(state, STAGES[2], "repositories", repo.key)
        read_json(plan_path)
        directory = artifact_path(state, STAGES[3], "repositories", repo.key).parent
        directory.mkdir(parents=True, exist_ok=True)
        policy_path = directory / "migration-policy.json"
        write_json(policy_path, policy)
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / "migrate.py"), repo.source,
                   "--policy", str(policy_path), "--recipe-repository", recipe_repository,
                   "--output", str(directory / "worktree"), "--workspace", str(directory / "engine")]
        command.append("--force")
        if repo.ref:
            # The legacy JSON manifest is the only path that carries refs.
            manifest = directory / "repository.json"
            write_json(manifest, [{"url": repo.source, "ref": repo.ref}])
            command[2:3] = ["--manifest", str(manifest)]
        result: dict[str, Any] = {
            "schema_version": 1, "artifact_type": "repository-migration-result",
            "stage": "04-migration", "generated_at": now(), "repository": repo.key,
            "status": "planned", "executed": False, "command": command,
            "inputs": {"plan": str(plan_path), "policy": str(policy_path)},
        }
        if execute:
            completed = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                       text=True, env=os.environ.copy(), check=False)
            result.update({"executed": True, "exit_code": completed.returncode,
                           "status": "complete" if completed.returncode == 0 else "failed"})
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
                "status": ("incomplete-cohort" if not complete_cohort else
                           "failed" if any(item["status"] == "failed" for item in results) else
                           "complete" if results and all(item["status"] == "complete" for item in results)
                           else "planned"),
                "results": [{"repository": item["repository"], "status": item["status"],
                             "executed": item["executed"]} for item in results],
            }
            write_json(artifact_path(state, STAGES[3], kind, key), summary)
            outputs.append(summary)
    return outputs


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        config = load_config(args.config.resolve())
        portfolio = load_portfolio(args.portfolio.resolve())
        errors = validate_targets(config)
        if errors:
            raise PortfolioError("invalid target configuration:\n- " + "\n- ".join(errors))
        if args.command == "validate":
            print(f"Valid: {len(portfolio.repositories)} repositories, "
                  f"{len({item.application_id for item in portfolio.repositories})} applications, "
                  f"{len({item.application_group_id for item in portfolio.repositories})} application groups")
            return 0
        selected = _selected(args, portfolio)
        state = args.state.resolve()
        if args.command == "report":
            outputs = render_reports(state, selected)
            print(f"Generated {len(outputs)} Markdown report(s) under {state / 'reports'}")
            return 0
        start = STAGES.index(args.from_stage)
        end = STAGES.index(args.through)
        if start > end:
            raise PortfolioError("--from must not come after --through")
        stages = STAGES[start:end + 1]
        discoveries: list[dict[str, Any]] = []
        if STAGES[0] in stages:
            for repo in selected:
                data = discover_repository(repo, config, state, args.refresh)
                write_json(artifact_path(state, STAGES[0], "repositories", repo.key), data)
                discoveries.append(data)
                print(f"[{STAGES[0]}] {repo.key}: {data['status']}")
            summarize_discoveries(selected, portfolio, discoveries, state)
        else:
            discoveries = [read_json(artifact_path(state, STAGES[0], "repositories", repo.key))
                           for repo in selected]
        if STAGES[1] in stages:
            results = assess(selected, portfolio, discoveries, config, state)
            print(f"[{STAGES[1]}] wrote {len(results)} assessment artifact(s)")
        if STAGES[2] in stages:
            results = plan(selected, portfolio, config, state)
            print(f"[{STAGES[2]}] wrote {len(results)} plan artifact(s)")
        if STAGES[3] in stages:
            results = migration_stage(selected, portfolio, config, state, args.execute)
            print(f"[{STAGES[3]}] {'executed' if args.execute else 'planned'} {len(selected)} repository migration(s)")
        reports = render_reports(state, selected)
        run_record = {
            "schema_version": 1, "generated_at": now(), "selected": [repo.key for repo in selected],
            "stages": list(stages), "execute": args.execute, "reports": [str(path) for path in reports],
        }
        write_json(state / "runs" / f"{run_record['generated_at'].replace(':', '-')}.json", run_record)
        return 0
    except (PortfolioError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
