"""Configuration, discovery, assessment, planning, and reporting primitives."""

from __future__ import annotations

import dataclasses
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Sequence

import java_migrator as legacy


STAGES = ("01-discovery", "02-assessment", "03-planning", "04-migration", "05-validation", "06-publishing")


class PortfolioError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class Repository:
    repo_name: str
    source: str
    application_id: str
    application_group_id: str
    repo_id: str | None = None
    ref: str | None = None
    role: str = "service"
    depends_on: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return self.repo_id or self.repo_name


@dataclasses.dataclass(frozen=True)
class Portfolio:
    path: Path
    repositories: tuple[Repository, ...]


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise PortfolioError(f"file does not exist: {path}")
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PortfolioError("YAML input requires PyYAML: python3 -m pip install PyYAML") from exc
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PortfolioError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PortfolioError(f"YAML root must be a mapping: {path}")
    return value


def load_config(path: Path) -> dict[str, Any]:
    data = load_yaml(path)
    errors: list[str] = []
    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    targets = data.get("targets")
    if not isinstance(targets, dict):
        errors.append("targets must be a mapping")
        targets = {}
    for name in ("java", "spring_boot"):
        target = targets.get(name)
        if not isinstance(target, dict):
            errors.append(f"targets.{name} must be a mapping")
            continue
        if target.get("desired") is None:
            errors.append(f"targets.{name}.desired is required")
        acceptable = target.get("acceptable")
        if not isinstance(acceptable, list) or not acceptable:
            errors.append(f"targets.{name}.acceptable must be a non-empty list")
    if errors:
        raise PortfolioError("invalid configuration:\n- " + "\n- ".join(errors))
    return data


def load_portfolio(path: Path) -> Portfolio:
    data = load_yaml(path)
    errors: list[str] = []
    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    raw_repositories = data.get("repositories")
    if not isinstance(raw_repositories, list) or not raw_repositories:
        errors.append("repositories must be a non-empty list")
        raw_repositories = []
    repositories: list[Repository] = []
    for index, raw in enumerate(raw_repositories):
        label = f"repositories[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{label} must be a mapping")
            continue
        required = ("repo_name", "source", "application_id", "application_group_id")
        missing = [key for key in required if not isinstance(raw.get(key), str) or not raw[key].strip()]
        if missing:
            errors.append(f"{label} is missing string fields: {', '.join(missing)}")
            continue
        depends_on = raw.get("depends_on", [])
        if not isinstance(depends_on, list) or not all(isinstance(item, str) for item in depends_on):
            errors.append(f"{label}.depends_on must be a list of repository ids/names")
            depends_on = []
        source = legacy.sanitized_url(str(raw["source"]))
        if not legacy.is_remote(source):
            source = str((path.parent / source).resolve()) if not Path(source).is_absolute() else source
        repositories.append(Repository(
            repo_name=raw["repo_name"].strip(), source=source,
            application_id=raw["application_id"].strip(),
            application_group_id=raw["application_group_id"].strip(),
            repo_id=(str(raw["repo_id"]).strip() if raw.get("repo_id") else None),
            ref=(str(raw["ref"]).strip() if raw.get("ref") else None),
            role=str(raw.get("role", "service")), depends_on=tuple(depends_on),
        ))
    keys = [repo.key for repo in repositories]
    names = [repo.repo_name for repo in repositories]
    for label, values in (("repository key", keys), ("repo_name", names)):
        duplicates = sorted(value for value, count in Counter(values).items() if count > 1)
        if duplicates:
            errors.append(f"duplicate {label}: {', '.join(duplicates)}")
    aliases: dict[str, str] = {}
    for repo in repositories:
        for alias in (repo.key, repo.repo_name):
            if alias in aliases and aliases[alias] != repo.key:
                errors.append(f"ambiguous repository identifier: {alias}")
            aliases[alias] = repo.key
        for value in (repo.key, repo.repo_name, repo.application_id, repo.application_group_id):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) or value in {".", ".."}:
                errors.append(f"unsafe artifact identifier: {value}")
    known = set(keys) | set(names)
    for repo in repositories:
        missing = sorted(set(repo.depends_on) - known)
        if missing:
            errors.append(f"{repo.key}.depends_on contains unknown repositories: {', '.join(missing)}")
    application_groups: dict[str, set[str]] = defaultdict(set)
    for repo in repositories:
        application_groups[repo.application_id].add(repo.application_group_id)
    for application, groups in application_groups.items():
        if len(groups) > 1:
            errors.append(f"application {application} belongs to multiple groups: {', '.join(sorted(groups))}")
    if errors:
        raise PortfolioError("invalid repository portfolio:\n- " + "\n- ".join(errors))
    return Portfolio(path.resolve(), tuple(repositories))


def validate_targets(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for name, target in config["targets"].items():
        if not isinstance(target, dict) or "desired" not in target or "acceptable" not in target:
            continue
        if not any(version_matches(str(target["desired"]), str(pattern)) for pattern in target["acceptable"]):
            errors.append(f"targets.{name}.desired must match at least one acceptable version")
    return errors


def select_repositories(
    portfolio: Portfolio, *, repos: Sequence[str], applications: Sequence[str],
    groups: Sequence[str], all_repositories: bool,
) -> list[Repository]:
    supplied = sum(bool(item) for item in (repos, applications, groups)) + bool(all_repositories)
    if supplied > 1:
        raise PortfolioError("choose exactly one selector type: --repo, --application, --application-group, or --all")
    if not supplied:
        all_repositories = True
    result = [repo for repo in portfolio.repositories if (
        all_repositories
        or repo.key in repos or repo.repo_name in repos
        or repo.application_id in applications
        or repo.application_group_id in groups
    )]
    if not result:
        raise PortfolioError("selector matched no repositories")
    return result


def artifact_path(state: Path, stage: str, kind: str, key: str) -> Path:
    return state / stage / kind / key / ("plan.json" if stage == "03-planning" else "result.json")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise PortfolioError(f"required upstream artifact is missing: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PortfolioError(f"cannot read artifact {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PortfolioError(f"artifact root must be an object: {path}")
    return data


def _command(command: Sequence[str], cwd: Path, *, timeout: int = 300,
             env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(command, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=True)
        return result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", "") or ""
        raise PortfolioError(f"command failed: {' '.join(command)}: {str(detail).strip()}") from exc


def prepare_discovery_source(repo: Repository, state: Path, refresh: bool) -> Path:
    if not legacy.is_remote(repo.source):
        path = Path(repo.source).expanduser().resolve()
        if not path.is_dir():
            raise PortfolioError(f"local repository does not exist: {path}")
        return path
    checkout = state / "repositories" / repo.key
    cloned = False
    git_env = os.environ.copy()
    git_env["GIT_TERMINAL_PROMPT"] = "0"
    if git_env.get("GIT_TOKEN"):
        askpass = state / ".git-askpass.sh"
        if not askpass.exists():
            askpass.parent.mkdir(parents=True, exist_ok=True)
            askpass.write_text(
                "#!/bin/sh\ncase \"$1\" in\n"
                "*sername*) printf '%s\\n' \"${MIGRATOR_GIT_USERNAME:-x-access-token}\" ;;\n"
                "*) printf '%s\\n' \"${MIGRATOR_GIT_TOKEN:-}\" ;;\nesac\n",
                encoding="utf-8",
            )
            askpass.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        git_env.update({"GIT_ASKPASS": str(askpass), "MIGRATOR_GIT_TOKEN": git_env["GIT_TOKEN"],
                        "MIGRATOR_GIT_USERNAME": git_env.get("GIT_USERNAME", "x-access-token")})
    if not checkout.exists():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        command = ["git", "clone", "--no-tags", repo.source, str(checkout)]
        _command(command, state, env=git_env)
        cloned = True
    elif not (checkout / ".git").is_dir():
        raise PortfolioError(f"managed checkout is not a Git repository: {checkout}")
    if refresh:
        _command(["git", "fetch", "--prune", "origin"], checkout, env=git_env)
    if repo.ref:
        if refresh or cloned:
            _command(["git", "fetch", "origin", repo.ref], checkout, env=git_env)
        _command(["git", "checkout", "--detach", "FETCH_HEAD"], checkout)
    elif refresh:
        remote_head = _command(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], checkout)
        _command(["git", "checkout", "--detach", remote_head], checkout)
    return checkout


def _local_name(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _resolve(value: str, properties: dict[str, str]) -> str:
    match = re.fullmatch(r"\$\{([^}]+)}", value.strip())
    return properties.get(match.group(1), value) if match else value


def inspect_maven(build: Path) -> tuple[set[str], set[str]]:
    java: set[str] = set()
    spring: set[str] = set()
    for pom in build.rglob("pom.xml"):
        try:
            root = ET.parse(pom).getroot()
        except (OSError, ET.ParseError):
            continue
        properties: dict[str, str] = {}
        for node in root.iter():
            if _local_name(node) == "properties":
                properties.update({_local_name(child): (child.text or "").strip() for child in node})
        for key in ("java.version", "maven.compiler.release", "maven.compiler.source", "maven.compiler.target"):
            if properties.get(key):
                java.add(_resolve(properties[key], properties).removeprefix("1."))
        parent = next((node for node in root if _local_name(node) == "parent"), None)
        if parent is not None:
            values = {_local_name(child): (child.text or "").strip() for child in parent}
            if values.get("groupId") == "org.springframework.boot" and values.get("version"):
                spring.add(_resolve(values["version"], properties))
        for node in root.iter():
            if _local_name(node) not in {"dependency", "plugin"}:
                continue
            values = {_local_name(child): (child.text or "").strip() for child in node}
            if values.get("groupId") == "org.springframework.boot" and values.get("version"):
                spring.add(_resolve(values["version"], properties))
    return java, spring


def inspect_gradle(build: Path) -> tuple[set[str], set[str]]:
    java: set[str] = set()
    spring: set[str] = set()
    files = [path for pattern in ("build.gradle", "build.gradle.kts", "gradle.properties")
             for path in build.rglob(pattern)]
    for path in files:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in (
            r"(?:sourceCompatibility|targetCompatibility)\s*=\s*(?:JavaVersion\.VERSION_)?['\"]?(?:1[._])?(\d+)",
            r"JavaLanguageVersion\.of\((\d+)\)", r"jvmToolchain\((\d+)\)",
        ):
            java.update(re.findall(pattern, content))
        spring.update(re.findall(
            r"(?:id\s*\(?\s*['\"]org\.springframework\.boot['\"]\s*\)?\s*version\s*['\"]|"
            r"org\.springframework\.boot(?:\s+version)?\s*=\s*)([0-9][^'\"\s]+)", content,
        ))
    return java, spring


def discover_repository(repo: Repository, config: dict[str, Any], state: Path, refresh: bool) -> dict[str, Any]:
    path = prepare_discovery_source(repo, state, refresh)
    git: dict[str, Any] = {"is_git_repository": (path / ".git").exists()}
    if git["is_git_repository"]:
        for key, command in {
            "commit": ["git", "rev-parse", "HEAD"],
            "branch": ["git", "branch", "--show-current"],
            "origin": ["git", "remote", "get-url", "origin"],
            "dirty": ["git", "status", "--porcelain"],
        }.items():
            try:
                value = _command(command, path, timeout=60)
                git[key] = bool(value) if key == "dirty" else value
            except PortfolioError:
                git[key] = None
    requested_tool = config.get("discovery", {}).get("build_tool", "auto")
    max_depth = int(config.get("discovery", {}).get("max_depth", 5))
    try:
        roots = legacy.discover_builds(path, requested_tool, max_depth)
    except legacy.MigrationError:
        roots = []
    projects: list[dict[str, Any]] = []
    all_java: set[str] = set()
    all_spring: set[str] = set()
    all_dependencies: list[dict[str, str]] = []
    analysis_args = type("AnalysisArgs", (), {"exclusions": tuple(legacy.DEFAULT_EXCLUSIONS)})()
    for root in roots:
        java, spring = inspect_maven(root.path) if root.tool == "maven" else inspect_gradle(root.path)
        analysis = legacy.analyze_project(root, analysis_args)
        dependencies = [dataclasses.asdict(item) | {"coordinate": item.coordinate}
                        for item in analysis.dependencies]
        relative = str(root.path.relative_to(path)) or "."
        projects.append({
            "path": relative, "build_tool": root.tool,
            "java_versions": sorted(java), "spring_boot_versions": sorted(spring),
            "features": analysis.features, "dependencies": dependencies,
            "findings": analysis.findings,
            "external_configuration": analysis.external_configuration,
        })
        all_java.update(java)
        all_spring.update(spring)
        all_dependencies.extend(dependencies)
    unique_dependencies = {(
        item["group"], item["artifact"], item["version"], item.get("configuration", "")
    ): item for item in all_dependencies}
    source_fingerprint = hashlib.sha256(json.dumps({
        "repo": dataclasses.asdict(repo), "git": git, "projects": projects,
    }, sort_keys=True).encode()).hexdigest()
    return {
        "schema_version": 1, "artifact_type": "repository-discovery",
        "stage": "01-discovery", "generated_at": now(), "status": "complete",
        "input_fingerprint": source_fingerprint,
        "repository": dataclasses.asdict(repo) | {"key": repo.key},
        "checkout": {"path": str(path), **git},
        "summary": {
            "build_count": len(projects), "build_tools": sorted({item["build_tool"] for item in projects}),
            "java_versions": sorted(all_java), "spring_boot_versions": sorted(all_spring),
            "dependency_count": len(unique_dependencies),
        },
        "projects": projects,
        "dependencies": sorted(unique_dependencies.values(), key=lambda item: (item["coordinate"], item["version"])),
        "issues": [] if projects else ["No Maven or Gradle build root was found."],
    }


def summarize_discoveries(
    selected: Sequence[Repository], portfolio: Portfolio,
    discoveries: Sequence[dict[str, Any]], state: Path,
) -> list[dict[str, Any]]:
    """Persist application and group views at the discovery boundary."""
    by_key = {item["repository"]["key"]: item for item in discoveries}
    outputs: list[dict[str, Any]] = []
    for kind, field in (("applications", "application_id"),
                        ("application-groups", "application_group_id")):
        for key in sorted({getattr(repo, field) for repo in selected}):
            members = [repo for repo in selected if getattr(repo, field) == key]
            items = [by_key[repo.key] for repo in members]
            result = {
                "schema_version": 1, "artifact_type": f"{kind[:-1]}-discovery",
                "stage": "01-discovery", "generated_at": now(), "status": "complete",
                "id": key,
                "cohort_complete": cohort_complete(selected, portfolio.repositories, field, key),
                "repositories": [{
                    "key": item["repository"]["key"],
                    "repo_name": item["repository"]["repo_name"],
                    **item["summary"],
                } for item in items],
                "summary": {
                    "repository_count": len(items),
                    "java_versions": sorted({v for item in items for v in item["summary"]["java_versions"]}),
                    "spring_boot_versions": sorted({v for item in items for v in item["summary"]["spring_boot_versions"]}),
                    "build_tools": sorted({v for item in items for v in item["summary"]["build_tools"]}),
                },
            }
            write_json(artifact_path(state, STAGES[0], kind, key), result)
            outputs.append(result)
    return outputs


def version_matches(version: str, pattern: str) -> bool:
    normalized = pattern.replace(".x", ".*").replace(".X", ".*")
    return fnmatch.fnmatch(version, normalized)


def version_status(versions: Sequence[str], target: dict[str, Any]) -> dict[str, Any]:
    desired = str(target["desired"])
    acceptable = [str(item) for item in target["acceptable"]]
    if not versions:
        status = "unknown"
    elif len(set(versions)) > 1:
        status = "inconsistent"
    elif version_matches(versions[0], desired):
        status = "desired"
    elif any(version_matches(versions[0], item) for item in acceptable):
        status = "acceptable"
    else:
        status = "upgrade-required"
    return {"status": status, "current": sorted(set(versions)), "desired": desired,
            "acceptable": acceptable}


def dependency_matrix(discoveries: Sequence[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    alignment = config.get("alignment", {}).get("dependencies", {})
    includes = alignment.get("include", ["*"])
    excludes = alignment.get("exclude", [])
    pins = alignment.get("pins", {})
    matrix: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for discovery in discoveries:
        key = discovery["repository"]["key"]
        for dependency in discovery.get("dependencies", []):
            coordinate = dependency["coordinate"]
            if (any(fnmatch.fnmatch(coordinate, item) for item in includes)
                    and not any(fnmatch.fnmatch(coordinate, item) for item in excludes)):
                matrix[coordinate][key].add(dependency["version"])
    mismatches: list[dict[str, Any]] = []
    for coordinate, repositories in sorted(matrix.items()):
        versions = sorted({version for values in repositories.values() for version in values})
        if len(repositories) > 1 and len(versions) > 1:
            from .policy import resolve_pin
            pin = resolve_pin(coordinate, pins)
            mismatches.append({
                "coordinate": coordinate, "severity": "error", "versions": versions,
                "repositories": {key: sorted(values) for key, values in sorted(repositories.items())},
                "target": pin, "resolution": "use configured pin" if pin else "decision-required",
            })
    return mismatches


def cohort_complete(selected: Sequence[Repository], all_repositories: Sequence[Repository], field: str, value: str) -> bool:
    selected_keys = {repo.key for repo in selected if getattr(repo, field) == value}
    all_keys = {repo.key for repo in all_repositories if getattr(repo, field) == value}
    return selected_keys == all_keys


def assess(
    selected: Sequence[Repository], portfolio: Portfolio, discoveries: Sequence[dict[str, Any]],
    config: dict[str, Any], state: Path,
) -> list[dict[str, Any]]:
    targets = config["targets"]
    by_key = {item["repository"]["key"]: item for item in discoveries}
    app_discoveries: dict[str, list[dict[str, Any]]] = defaultdict(list)
    group_discoveries: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for repo in selected:
        app_discoveries[repo.application_id].append(by_key[repo.key])
        group_discoveries[repo.application_group_id].append(by_key[repo.key])
    outputs: list[dict[str, Any]] = []
    app_mismatches = {key: dependency_matrix(items, config) for key, items in app_discoveries.items()}
    group_mismatches = {key: dependency_matrix(items, config) for key, items in group_discoveries.items()}
    for repo in selected:
        discovery = by_key[repo.key]
        java = version_status(discovery["summary"]["java_versions"], targets["java"])
        spring_present = bool(discovery["summary"]["spring_boot_versions"]) or any(
            "spring-boot" in project.get("features", []) for project in discovery.get("projects", [])
        )
        spring = (version_status(discovery["summary"]["spring_boot_versions"], targets["spring_boot"])
                  if spring_present else {
                      "status": "not-applicable", "current": [],
                      "desired": str(targets["spring_boot"]["desired"]),
                      "acceptable": [str(item) for item in targets["spring_boot"]["acceptable"]],
                  })
        findings: list[dict[str, str]] = []
        for name, status in (("java", java), ("spring_boot", spring)):
            if status["status"] in {"unknown", "inconsistent", "upgrade-required"}:
                findings.append({"severity": "error" if status["status"] != "unknown" else "warning",
                                 "category": name, "message": f"{name} is {status['status']}"})
        relevant = [item for item in app_mismatches[repo.application_id]
                    if repo.key in item["repositories"]]
        assessment = {
            "schema_version": 1, "artifact_type": "repository-assessment",
            "stage": "02-assessment", "generated_at": now(),
            "status": "attention-required" if findings or relevant else "ready",
            "repository": discovery["repository"],
            "inputs": {"discovery": str(artifact_path(state, STAGES[0], "repositories", repo.key))},
            "versions": {"java": java, "spring_boot": spring},
            "dependency_mismatches": relevant, "findings": findings,
        }
        write_json(artifact_path(state, STAGES[1], "repositories", repo.key), assessment)
        outputs.append(assessment)
    for kind, values, mismatch_map, field in (
        ("applications", app_discoveries, app_mismatches, "application_id"),
        ("application-groups", group_discoveries, group_mismatches, "application_group_id"),
    ):
        for key, items in values.items():
            complete = cohort_complete(selected, portfolio.repositories, field, key)
            java_versions = sorted({v for item in items for v in item["summary"]["java_versions"]})
            spring_versions = sorted({v for item in items for v in item["summary"]["spring_boot_versions"]})
            spring_present = bool(spring_versions) or any(
                "spring-boot" in project.get("features", [])
                for item in items for project in item.get("projects", [])
            )
            alignment = {
                "java": {"aligned": len(java_versions) <= 1, "versions": java_versions},
                "spring_boot": {"aligned": len(spring_versions) <= 1, "versions": spring_versions},
                "dependencies": mismatch_map[key],
            }
            target_compliance = {
                "java": version_status(java_versions, targets["java"]),
                "spring_boot": (version_status(spring_versions, targets["spring_boot"])
                                if spring_present else {
                                    "status": "not-applicable", "current": [],
                                    "desired": str(targets["spring_boot"]["desired"]),
                                    "acceptable": [str(item) for item in targets["spring_boot"]["acceptable"]],
                                }),
            }
            needs_attention = (
                not alignment["java"]["aligned"]
                or not alignment["spring_boot"]["aligned"]
                or bool(alignment["dependencies"])
                or any(item["status"] not in {"desired", "acceptable", "not-applicable"}
                       for item in target_compliance.values())
            )
            result = {
                "schema_version": 1, "artifact_type": f"{kind[:-1]}-assessment",
                "stage": "02-assessment", "generated_at": now(), "id": key,
                "cohort_complete": complete,
                "status": ("incomplete-cohort" if not complete else
                           "attention-required" if needs_attention else "ready"),
                "repositories": sorted(item["repository"]["key"] for item in items),
                "target_compliance": target_compliance, "alignment": alignment,
            }
            write_json(artifact_path(state, STAGES[1], kind, key), result)
            outputs.append(result)
    return outputs


def _task(task_id: str, text: str, *, status: str = "pending", details: Any = None,
          blocked_by: Sequence[str] = ()) -> dict[str, Any]:
    result: dict[str, Any] = {"id": task_id, "status": status, "checklist": text,
                              "blocked_by": list(blocked_by)}
    if details is not None:
        result["details"] = details
    return result


def migration_waves(repositories: Sequence[Repository]) -> tuple[list[list[str]], list[str]]:
    """Order selected repositories; dependencies outside the selection are out of scope."""
    aliases = {alias: repo.key for repo in repositories for alias in (repo.key, repo.repo_name)}
    dependencies = {
        repo.key: {aliases[dep] for dep in repo.depends_on if dep in aliases}
        for repo in repositories
    }
    remaining = set(dependencies)
    completed: set[str] = set()
    waves: list[list[str]] = []
    ordering_issues: list[str] = []
    while remaining:
        ready = sorted(key for key in remaining if dependencies[key] <= completed)
        if not ready:
            ready = sorted(remaining)
            ordering_issues.append(
                "Dependency cycle detected among: " + ", ".join(ready)
                + "; manual wave ordering is required."
            )
        waves.append(ready)
        completed.update(ready)
        remaining.difference_update(ready)
    return waves, ordering_issues


def plan(selected: Sequence[Repository], portfolio: Portfolio, config: dict[str, Any], state: Path) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for repo in selected:
        assessment_path = artifact_path(state, STAGES[1], "repositories", repo.key)
        assessment = read_json(assessment_path)
        tasks = [_task("review-discovery", "Review and approve the discovery and assessment artifacts.")]
        if assessment["versions"]["java"]["status"] != "desired":
            tasks.append(_task("upgrade-java", f"Update Java to {config['targets']['java']['desired']}.",
                               details=assessment["versions"]["java"], blocked_by=("review-discovery",)))
        if assessment["versions"]["spring_boot"]["status"] not in {"desired", "unknown", "not-applicable"}:
            tasks.append(_task("upgrade-spring-boot",
                               f"Update Spring Boot to {config['targets']['spring_boot']['desired']} as one managed stack.",
                               details=assessment["versions"]["spring_boot"], blocked_by=("upgrade-java",)))
        for index, mismatch in enumerate(assessment["dependency_mismatches"], 1):
            target = mismatch.get("target") or "an approved common version"
            tasks.append(_task(f"align-dependency-{index}",
                               f"Align {mismatch['coordinate']} to {target} across the application.",
                               details=mismatch, blocked_by=("review-discovery",)))
        tasks += [
            _task("build-and-test", "Run compile, unit, integration, and contract checks.",
                  blocked_by=tuple(item["id"] for item in tasks if item["id"] != "review-discovery")),
            _task("verify-alignment", "Rerun discovery and assessment; require no application version mismatches.",
                  blocked_by=("build-and-test",)),
        ]
        result = {
            "schema_version": 1, "artifact_type": "repository-migration-plan",
            "stage": "03-planning", "generated_at": now(), "status": "planned",
            "repository": assessment["repository"], "inputs": {"assessment": str(assessment_path)},
            "tasks": tasks, "completion_rule": "Every task is complete and a rerun reports desired/acceptable aligned versions.",
        }
        write_json(artifact_path(state, STAGES[2], "repositories", repo.key), result)
        outputs.append(result)
    for kind, field in (("applications", "application_id"), ("application-groups", "application_group_id")):
        ids = sorted({getattr(repo, field) for repo in selected})
        for key in ids:
            members = [repo for repo in selected if getattr(repo, field) == key]
            assessment = read_json(artifact_path(state, STAGES[1], kind, key))
            member_keys = {repo.key for repo in members}
            waves, ordering_issues = migration_waves(members)
            tasks = [
                _task("approve-targets", "Approve common Java, Spring Boot, and dependency targets."),
                _task("migrate-waves", "Migrate repositories in dependency order, one wave at a time.",
                      details={"waves": waves}, blocked_by=("approve-targets",)),
                _task("verify-cohort", "Rerun the complete cohort and resolve every alignment mismatch.",
                      blocked_by=("migrate-waves",)),
            ]
            result = {
                "schema_version": 1, "artifact_type": f"{kind[:-1]}-migration-plan",
                "stage": "03-planning", "generated_at": now(), "status": "planned", "id": key,
                "cohort_complete": assessment["cohort_complete"], "repositories": sorted(member_keys),
                "inputs": {"assessment": str(artifact_path(state, STAGES[1], kind, key))},
                "ordering_issues": ordering_issues,
                "migration_waves": waves, "tasks": tasks,
            }
            write_json(artifact_path(state, STAGES[2], kind, key), result)
            outputs.append(result)
    return outputs


def markdown_for_plan(plan_data: dict[str, Any]) -> str:
    identity = plan_data.get("repository", {}).get("key") or plan_data.get("id", "portfolio")
    title = plan_data["artifact_type"].replace("-", " ").title()
    lines = [f"# {title}: {identity}", "", "> Generated view. JSON is the source of truth.", "",
             f"- Generated: `{plan_data['generated_at']}`", f"- Status: **{plan_data['status']}**"]
    if "cohort_complete" in plan_data:
        lines.append(f"- Complete cohort: **{'yes' if plan_data['cohort_complete'] else 'no'}**")
    if plan_data.get("migration_waves"):
        lines += ["", "## Migration waves", ""]
        for index, wave in enumerate(plan_data["migration_waves"], 1):
            lines.append(f"{index}. {', '.join(f'`{item}`' for item in wave)}")
    if plan_data.get("ordering_issues"):
        lines += ["", "## Ordering issues", ""]
        lines += [f"- [ ] {item}" for item in plan_data["ordering_issues"]]
    lines += ["", "## Checklist", ""]
    for task in plan_data.get("tasks", []):
        marker = "x" if task.get("status") == "complete" else " "
        lines.append(f"- [{marker}] **{task['id']}** — {task['checklist']}")
        if task.get("blocked_by"):
            lines.append(f"  - Blocked by: {', '.join(f'`{item}`' for item in task['blocked_by'])}")
    if plan_data.get("completion_rule"):
        lines += ["", "## Completion rule", "", plan_data["completion_rule"]]
    return "\n".join(lines) + "\n"


def markdown_for_assessment(data: dict[str, Any]) -> str:
    identity = data.get("repository", {}).get("key") or data.get("id", "portfolio")
    title = data["artifact_type"].replace("-", " ").title()
    lines = [f"# {title}: {identity}", "", "> Generated view. JSON is the source of truth.", "",
             f"- Generated: `{data['generated_at']}`", f"- Status: **{data['status']}**"]
    if "cohort_complete" in data:
        lines.append(f"- Complete cohort: **{'yes' if data['cohort_complete'] else 'no'}**")
    versions = data.get("versions", data.get("target_compliance", {}))
    if versions:
        lines += ["", "## Version checklist", ""]
        for name, value in versions.items():
            checked = value["status"] in {"desired", "acceptable", "not-applicable"}
            lines.append(f"- [{'x' if checked else ' '}] {name.replace('_', ' ').title()}: "
                         f"current `{', '.join(value['current']) or 'unknown'}`; desired `{value['desired']}`; "
                         f"status **{value['status']}**")
    mismatches = data.get("dependency_mismatches", data.get("alignment", {}).get("dependencies", []))
    if mismatches:
        lines += ["", "## Dependency alignment", ""]
        for item in mismatches:
            lines.append(f"- [ ] `{item['coordinate']}`: {', '.join(item['versions'])}; "
                         f"target `{item.get('target') or 'decision required'}`")
    return "\n".join(lines) + "\n"


def markdown_for_discovery(data: dict[str, Any]) -> str:
    identity = data.get("repository", {}).get("key") or data.get("id", "portfolio")
    title = data["artifact_type"].replace("-", " ").title()
    lines = [f"# {title}: {identity}", "", "> Generated view. JSON is the source of truth.", "",
             f"- Generated: `{data['generated_at']}`", f"- Status: **{data['status']}**"]
    if "cohort_complete" in data:
        lines.append(f"- Complete cohort: **{'yes' if data['cohort_complete'] else 'no'}**")
    if data.get("repository"):
        summary = data.get("summary", {})
        lines += ["", "## Current state", "",
                  f"- [ ] Build roots inspected: **{summary.get('build_count', 0)}**",
                  f"- [ ] Java versions: `{', '.join(summary.get('java_versions', [])) or 'unknown'}`",
                  f"- [ ] Spring Boot versions: `{', '.join(summary.get('spring_boot_versions', [])) or 'not detected'}`",
                  f"- [ ] Direct dependencies recorded: **{summary.get('dependency_count', 0)}**"]
    elif data.get("repositories"):
        lines += ["", "## Repositories", "",
                  "| Repository | Build tools | Java | Spring Boot | Dependencies |",
                  "| --- | --- | --- | --- | ---: |"]
        for item in data["repositories"]:
            lines.append(f"| `{item['key']}` | {', '.join(item['build_tools']) or '—'} | "
                         f"{', '.join(item['java_versions']) or 'unknown'} | "
                         f"{', '.join(item['spring_boot_versions']) or 'not detected'} | "
                         f"{item['dependency_count']} |")
    return "\n".join(lines) + "\n"


def markdown_for_migration(data: dict[str, Any]) -> str:
    identity = data.get("repository") or data.get("id", "portfolio")
    title = data["artifact_type"].replace("-", " ").title()
    lines = [f"# {title}: {identity}", "", "> Generated view. JSON is the source of truth.", "",
             f"- Generated: `{data['generated_at']}`", f"- Status: **{data['status']}**"]
    if "cohort_complete" in data:
        lines.append(f"- Complete cohort: **{'yes' if data['cohort_complete'] else 'no'}**")
    if data.get("blocked_by"):
        lines.append("- Blocked by: " + ", ".join(f"`{key}`" for key in data["blocked_by"]))
    if "exit_code" in data:
        lines.append(f"- Engine exit code: `{data['exit_code']}`")
    if data.get("error"):
        lines.append(f"- Error: {data['error']}")
    if data.get("results"):
        lines += ["", "## Repository checklist", ""]
        for result in data["results"]:
            lines.append(f"- [{'x' if result['status'] in {'complete', 'migrated'} else ' '}] `{result['repository']}` — {result['status']}")
    else:
        lines += ["", "## Execution checklist", "",
                  f"- [{'x' if data.get('executed') else ' '}] Execute the generated migration command.",
                  f"- [{'x' if data.get('status') in {'complete', 'migrated'} else ' '}] Verify migration engine completion.",
                  "- [ ] Rerun discovery and assessment for the complete application cohort."]
    return "\n".join(lines) + "\n"


def markdown_for_validation(data: dict[str, Any]) -> str:
    if "cohort_complete" in data:
        return markdown_for_assessment(data)
    lines = [f"# Validation: {data['repository']}", "", "> Generated view. JSON is the source of truth.", "",
             f"- Status: **{data['status']}**"]
    for key in ("output", "commit", "tree_hash", "error"):
        if key in data:
            lines.append(f"- {key}: `{data[key]}`")
    lines += ["", "## Required checks", ""]
    for check in data.get("checks", []):
        lines.append(f"- [{'x' if check['status'] == 'passed' else ' '}] {check['name']}: `{' '.join(check['command'])}`")
    inventory = data.get("inventory", {})
    if inventory:
        lines += ["", f"- Effective Java: {', '.join(inventory['java_versions'])}",
                  f"- Effective Spring Boot: {', '.join(inventory['spring_boot_versions']) or 'not applicable'}",
                  f"- Resolved dependencies: {len(inventory['dependencies'])}"]
    return "\n".join(lines) + "\n"


def markdown_for_publishing(data: dict[str, Any]) -> str:
    lines = [f"# Publishing: {data['repository']}", "", "> Generated view. JSON is the source of truth.", "",
             f"- Status: **{data['status']}**", ""]
    if data.get("error"):
        lines.append(f"- Error: {data['error']}")
    for name, target in data.get("targets", {}).items():
        lines.append(f"- [{'x' if target['status'] == 'published' else ' '}] `{name}`: {target['status']}")
        for key in ("url", "branch", "commit", "error"):
            if key in target:
                lines.append(f"  - {key}: `{target[key]}`")
    return "\n".join(lines) + "\n"


def render_reports(state: Path, selected: Sequence[Repository] | None = None) -> list[Path]:
    selected_keys = {repo.key for repo in selected} if selected else None
    selected_applications = {repo.application_id for repo in selected} if selected else None
    selected_groups = {repo.application_group_id for repo in selected} if selected else None
    outputs: list[Path] = []
    report_root = state / "reports"
    for stage, renderer in (
        (STAGES[0], markdown_for_discovery), (STAGES[1], markdown_for_assessment),
        (STAGES[2], markdown_for_plan), (STAGES[3], markdown_for_migration),
        (STAGES[4], markdown_for_validation), (STAGES[5], markdown_for_publishing),
    ):
        root = state / stage
        if not root.exists():
            continue
        for source in sorted(root.glob("*/*/*.json")):
            data = read_json(source)
            if "artifact_type" not in data:
                continue
            if selected_keys is not None:
                kind, key = source.parts[-3], source.parts[-2]
                allowed = ({"repositories": selected_keys, "applications": selected_applications,
                            "application-groups": selected_groups}.get(kind))
                if allowed is not None and key not in allowed:
                    continue
            relative = source.relative_to(root).with_suffix(".md")
            destination = report_root / stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(renderer(data), encoding="utf-8")
            outputs.append(destination)
    summary = ["# Java update portfolio", "", "> Generated view. JSON and YAML are the source of truth.", ""]
    for stage in STAGES:
        count = (len(list((state / stage).glob("*/*/result.json")))
                 + len(list((state / stage).glob("*/*/plan.json")))) if (state / stage).exists() else 0
        summary.append(f"- [{'x' if count else ' '}] `{stage}` — {count} artifact(s)")
    path = report_root / "README.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(summary) + "\n", encoding="utf-8")
    outputs.append(path)
    return outputs
