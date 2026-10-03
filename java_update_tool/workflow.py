"""Six-stage, resumable portfolio command-line workflow."""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path
from typing import Any, Sequence

from . import cli
from .core import PortfolioError, STAGES, artifact_path, assess, discover_repository, load_config, load_portfolio, now, plan, read_json, render_reports, summarize_discoveries, validate_targets, write_json
from .policy import workflow_policy, target_recipes, resolve_pin, numeric_version, compatibility_hash, validate_location
from .runs import approve, canonical, check_approvals, compare_runs, create_run, run_directory, snapshots, git, files_digest, source_commit
from .validation import validate_stage
from .publishing import publish_stage


def parser() -> argparse.ArgumentParser:
    result = cli.parser()
    commands = next(action for action in result._actions if isinstance(action, argparse._SubParsersAction))
    run = commands.choices["run"]
    for action in run._actions:
        if action.dest in {"from_stage", "through"}:
            action.choices = STAGES
    run.set_defaults(from_stage=None)
    run.add_argument("--resume", metavar="RUN_ID", help="resume a retained run; use latest for the most recent")
    run.add_argument("--mode", choices=("manual", "unattended"))
    run.add_argument("--show-diffs", action=argparse.BooleanOptionalAction, default=None)
    run.add_argument("--include-dependencies", action=argparse.BooleanOptionalAction, default=None)
    run.add_argument("--dependency-override", action=argparse.BooleanOptionalAction, default=None)
    run.add_argument("--enable-publishing", action=argparse.BooleanOptionalAction, default=None)
    run.add_argument("--publish-target", action="append", choices=("local_repo", "src_repo", "dst_repo"))
    commands.choices["report"].add_argument("--run-id", default="latest")
    approval = commands.add_parser("approve", help="approve an unchanged completed stage")
    approval.add_argument("run_id")
    approval.add_argument("--stage", choices=STAGES, required=True)
    commands.add_parser("runs", help="list retained runs")
    diff = commands.add_parser("diff", help="compare source, output, and policy between runs")
    diff.add_argument("run_id")
    diff.add_argument("--compare-to", required=True)
    return result


def expand_dependencies(selected: list[Any], portfolio: Any) -> list[Any]:
    aliases = {alias: repo for repo in portfolio.repositories for alias in (repo.key, repo.repo_name)}
    keys = {repo.key for repo in selected}
    pending = list(selected)
    while pending:
        for name in pending.pop().depends_on:
            dependency = aliases[name]
            if dependency.key not in keys:
                pending.append(dependency)
                keys.add(dependency.key)
    return [repo for repo in portfolio.repositories if repo.key in keys]


def dependencies_ready(selected: Sequence[Any], portfolio: Any, options: dict[str, Any], root: Path) -> None:
    aliases = {alias: repo for repo in portfolio.repositories for alias in (repo.key, repo.repo_name)}
    keys = {repo.key for repo in selected}
    receipt = read_json(root / "run.json")
    for repo in selected:
        for name in repo.depends_on:
            dependency = aliases[name]
            if dependency.key in keys:
                continue
            evidence = options["dependency_evidence"].get(dependency.key)
            try:
                if not evidence:
                    raise PortfolioError("missing dependency evidence")
                record = read_json(Path(evidence).expanduser().resolve())
                output = Path(record["output"])
                if record["status"] != "validated" or git(output, "rev-parse", "HEAD") != record["commit"] or files_digest(output) != record["tree_hash"]:
                    raise PortfolioError("dependency evidence changed or failed")
                if record.get("repository") != dependency.key or record.get("compatibility_hash") != compatibility_hash(receipt["config"], options):
                    raise PortfolioError("dependency evidence is for another repository or policy")
                if record.get("source_commit") != source_commit(dependency):
                    raise PortfolioError("dependency source revision has changed since validation")
            except (PortfolioError, OSError, KeyError) as exc:
                if options["mode"] == "manual" and options["allow_dependency_override"]:
                    receipt["dependency_override"] = True
                else:
                    raise PortfolioError(f"{repo.key} needs validated compatibility evidence for {dependency.key}; use --include-dependencies") from exc
    write_json(root / "run.json", receipt)


def planning_policy(config: dict[str, Any], discoveries: list[dict[str, Any]], options: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(config)
    framework = any(item["summary"]["spring_boot_versions"] or any("spring-boot" in project["features"] for project in item["projects"]) for item in discoveries)
    recipes = target_recipes(config, framework=framework)
    if framework:
        result.setdefault("migration", {}).setdefault("openrewrite", {})["recipes"] = recipes
        artifacts = result["migration"]["openrewrite"].get("artifacts", [])
        if not any(item.startswith("org.openrewrite.recipe:rewrite-spring:") for item in artifacts):
            raise PortfolioError("Spring Boot migration requires a pinned rewrite-spring artifact in migration.openrewrite.artifacts; see notes/workflow.md")
    else:
        result.setdefault("migration", {}).setdefault("openrewrite", {})["recipes"] = [
            item for item in config.get("migration", {}).get("openrewrite", {}).get("recipes", []) if "UpgradeSpringBoot_" not in item]
    rewrite = result["migration"]["openrewrite"]
    if rewrite.get("artifacts"):
        import java_migrator as engine
        versions = engine.CODE_GENOME_VERSIONS if rewrite.get("recipe_repository") == "codegenome" else engine.MAVEN_CENTRAL_VERSIONS
        for artifact, key in (("rewrite-migrate-java", "migrate_java"), ("rewrite-static-analysis", "static_analysis"),
                              ("rewrite-java-dependencies", "java_dependencies"), ("rewrite-testing-frameworks", "testing_frameworks")):
            if not any(item.startswith(f"org.openrewrite.recipe:{artifact}:") for item in rewrite["artifacts"]):
                rewrite["artifacts"].append(f"org.openrewrite.recipe:{artifact}:{versions[key]}")
    pins = config.get("alignment", {}).get("dependencies", {}).get("pins", {})
    for discovery in discoveries:
        for dependency in discovery["dependencies"]:
            pinned = resolve_pin(dependency["coordinate"], pins)
            current = dependency["version"]
            if pinned and numeric_version(pinned) < numeric_version(current) and not options["allow_downgrades"]:
                raise PortfolioError(f"downgrade requires workflow.allow_downgrades: {dependency['coordinate']} {current} → {pinned}")
    return result


def checkpoint(root: Path, stage: str, options: dict[str, Any]) -> bool:
    receipt = read_json(root / "run.json")
    print(f"[{stage}] resources: {root / stage}")
    print(f"[{stage}] reports: {root / 'reports' / stage}")
    if options["mode"] == "unattended" or stage not in options["checkpoints"]:
        return True
    if stage in receipt["approvals"]:
        return True
    receipt["status"] = "awaiting-review"
    receipt["pending_review"] = stage
    write_json(root / "run.json", receipt)
    if sys.stdin.isatty():
        response = input(f"Approve {stage} and proceed? [y/N] ").strip().lower()
        if response in {"y", "yes"}:
            approve(root, stage)
            return True
    print(f"Paused. Review resources, then: portfolio.py --state {root.parents[1]} approve {root.name} --stage {stage}")
    return False


def main(argv: Sequence[str] | None = None) -> int:
    root = None
    try:
        args = parser().parse_args(argv)
        state = args.state.resolve()
        if args.command == "runs":
            for path in sorted((state / "runs").glob("*/run.json")):
                receipt = read_json(path)
                print(f"{receipt['run_id']} {receipt['status']} {','.join(receipt['selected'])}")
            return 0
        if args.command == "diff":
            print(compare_runs(run_directory(state, args.run_id), run_directory(state, args.compare_to)))
            return 0
        if args.command == "approve":
            root = run_directory(state, args.run_id)
            config = load_config(args.config.resolve())
            if canonical(config) != read_json(root / "run.json")["config_hash"]:
                raise PortfolioError("policy changed; start a new run")
            approve(root, args.stage)
            print(f"Approved {root.name}: {args.stage}")
            return 0
        config = load_config(args.config.resolve())
        portfolio = load_portfolio(args.portfolio.resolve())
        for repo in portfolio.repositories:
            validate_location(repo.source)
        errors = validate_targets(config)
        if errors:
            raise PortfolioError("invalid targets: " + "; ".join(errors))
        overrides = {}
        if args.command == "run":
            for name, field in (("mode", "mode"), ("show_diffs", "show_diffs"), ("include_dependencies", "include_dependencies"), ("dependency_override", "allow_dependency_override")):
                if getattr(args, name) is not None:
                    overrides[field] = getattr(args, name)
            if args.enable_publishing is not None:
                overrides["publishing"] = {"enabled": args.enable_publishing}
            if args.publish_target:
                overrides.setdefault("publishing", {})["targets"] = args.publish_target
        options = workflow_policy(config, overrides)
        for area in ("validation", "publishing"):
            unknown = set(options[area]["repositories"]) - {repo.key for repo in portfolio.repositories}
            if unknown:
                raise PortfolioError(f"unknown repository keys in workflow.{area}.repositories: {', '.join(sorted(unknown))}")
        target_recipes(config, framework=False)
        if args.command == "validate":
            print(f"Valid: {len(portfolio.repositories)} repositories; six-stage workflow, mode={options['mode']}")
            return 0
        selected = cli._selected(args, portfolio)
        if args.command == "report":
            print(f"Generated {len(render_reports(run_directory(state, args.run_id), selected))} reports")
            return 0
        if options["include_dependencies"]:
            selected = expand_dependencies(selected, portfolio)
        if args.resume:
            root = run_directory(state, args.resume)
            receipt = read_json(root / "run.json")
            if canonical(config) != receipt["config_hash"] or options != receipt["workflow"]:
                raise PortfolioError("source policy or CLI overrides changed; start a new run")
            if args.repo or args.application or args.application_group or args.all_repositories:
                if [repo.key for repo in selected] != receipt["selected"]:
                    raise PortfolioError("resume selection differs from saved run")
            selected = [repo for repo in portfolio.repositories if repo.key in receipt["selected"]]
            if [vars(repo) for repo in selected] != [{**item, "depends_on": tuple(item["depends_on"])} for item in receipt["repositories"]]:
                raise PortfolioError("repository definitions changed; start a new run")
            check_approvals(root)
        else:
            if args.from_stage and args.from_stage != STAGES[0]:
                raise PortfolioError("starting from an upstream stage requires --resume RUN_ID")
            root = create_run(state, selected, config, options)
        print(f"Run: {root.name}")
        receipt = read_json(root / "run.json")
        start = STAGES.index(args.from_stage) if args.from_stage else next((i for i, stage in enumerate(STAGES) if receipt["stages"].get(stage, {}).get("status") not in {"complete", "partial"} and not (receipt["stages"].get(stage, {}).get("status") == "prepared" and not args.execute)), len(STAGES))
        end = STAGES.index(args.through)
        if start > end and start != len(STAGES):
            # A paused checkpoint is still reviewable when all requested stages already ran.
            pending = receipt.get("pending_review")
            if pending and pending not in receipt["approvals"]:
                return 0 if checkpoint(root, pending, options) else 3
            if args.from_stage:
                raise PortfolioError("--from must not come after --through")
        pending = receipt.get("pending_review")
        if pending and pending not in receipt["approvals"] and not checkpoint(root, pending, options):
            return 3
        source_portfolio, sources = snapshots(root, portfolio, selected)
        discoveries = []
        for index in range(start, end + 1):
            stage = STAGES[index]
            check_approvals(root)
            current = read_json(root / "run.json")
            for later in STAGES[index:]:
                current["approvals"].pop(later, None)
                current["stages"].pop(later, None)
            write_json(root / "run.json", current)
            if index == 0:
                for repo in sources:
                    data = discover_repository(repo, config, root, False)
                    write_json(artifact_path(root, stage, "repositories", repo.key), data)
                    discoveries.append(data)
                summarize_discoveries(sources, source_portfolio, discoveries, root)
                outputs = discoveries
            else:
                if not discoveries:
                    discoveries = [read_json(artifact_path(root, STAGES[0], "repositories", repo.key)) for repo in selected]
                if index == 1:
                    outputs = assess(sources, source_portfolio, discoveries, config, root)
                elif index == 2:
                    proposed = planning_policy(config, discoveries, options)
                    write_json(root / "planned-policy.json", proposed)
                    outputs = plan(sources, source_portfolio, config, root)
                elif index == 3:
                    dependencies_ready(selected, portfolio, options, root)
                    outputs = cli.migration_stage(sources, source_portfolio, read_json(root / "planned-policy.json"), root, args.execute, managed=True)
                elif index == 4:
                    outputs = validate_stage(selected, portfolio, config, options, root)
                else:
                    if not args.execute:
                        raise PortfolioError("publishing requires --execute")
                    dependencies_ready(selected, portfolio, options, root)
                    outputs = publish_stage(selected, config, options, root)
            failed = any(item.get("status") in {"failed", "blocked"} for item in outputs)
            if index == 4:
                failed = any(item.get("status") not in {"validated", "ready"} for item in outputs)
            prepared = index == 3 and any(item.get("status") in {"planned", "analyzed"} for item in outputs)
            partial = index == 4 and failed and options["publishing"]["independent_applications"]
            receipt = read_json(root / "run.json")
            receipt["stages"][stage] = {"status": "partial" if partial else "failed" if failed else "prepared" if prepared else "complete", "finished_at": now()}
            receipt["status"] = "failed" if failed else "running"
            receipt["exit_code"] = 1 if failed else 0
            write_json(root / "run.json", receipt)
            render_reports(root, selected)
            if options["show_diffs"]:
                previous = [path.parent for path in sorted((state / "runs").glob("*/run.json")) if path.parent != root]
                if previous:
                    print(f"Run diff: {compare_runs(root, previous[-1])}")
            print(f"[{stage}] {receipt['stages'][stage]['status']}")
            if failed and index not in {3, 4}:
                return 1
            if (not failed or partial) and not checkpoint(root, stage, options):
                return 3
            if prepared:
                return 0
            if failed and (index != 4 or not options["publishing"]["independent_applications"]):
                return 1
        receipt = read_json(root / "run.json")
        receipt["status"] = "published" if STAGES[5] in receipt["stages"] and receipt["stages"][STAGES[5]]["status"] == "complete" else "complete"
        receipt["exit_code"] = 0
        write_json(root / "run.json", receipt)
        return 0
    except (PortfolioError, OSError, ValueError, KeyError) as exc:
        if root:
            receipt = read_json(root / "run.json")
            receipt.update({"status": "failed", "error": str(exc), "exit_code": 2})
            write_json(root / "run.json", receipt)
        print(f"error: {exc}", file=sys.stderr)
        return 2
