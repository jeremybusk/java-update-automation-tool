"""Validated workflow policy and deterministic version decisions."""
from __future__ import annotations

import copy
import fnmatch
import re
import urllib.parse
from typing import Any

from .core import PortfolioError, STAGES

DEFAULTS = {
    "mode": "manual", "checkpoints": list(STAGES), "show_diffs": False,
    "source_selection": "default", "source_credentials": {},
    "diagnostics": {"check_bytes": 10485760, "run_bytes": 104857600, "retention_days": 30},
    "retention": {"days": 30, "pins": []},
    "include_dependencies": False, "allow_dependency_override": False,
    "dependency_evidence": {}, "allow_downgrades": False,
    "validation": {"build": "test", "commands": [], "timeout": 3600, "repositories": {},
                   "build_roots": [], "exclusions": {}, "test_exemptions": {}, "suites": []},
    "publishing": {
        "enabled": False, "targets": ["local_repo"], "history": "default",
        "independent_applications": False, "provider": "github",
        "mode": "autocreate", "prefix": "", "owner": "", "namespace_id": None,
        "default_branch": "main", "source_branch": "automation/java-{java}-{run_id}",
        "request": {"enabled": False, "base": None, "links": []},
        "source_default_branch": None,
        "private": True, "api_url": None, "token_env": None, "repositories": {},
    },
}


def validate_commands(commands: Any) -> None:
    if not isinstance(commands, list) or any(
        not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command)
        for command in commands
    ):
        raise PortfolioError("workflow.validation.commands must contain non-empty argv lists")


def validate_validation(options: dict[str, Any]) -> None:
    if options["build"] not in {"compile", "test"} or type(options["timeout"]) is not int or options["timeout"] < 1:
        raise PortfolioError("validation requires build=compile/test and positive timeout")
    if not isinstance(options["build_roots"], list) or any(not isinstance(path, str) or invalid_relative_path(path) for path in options["build_roots"]):
        raise PortfolioError("validation.build_roots must contain relative build directories")
    for key in ("exclusions", "test_exemptions"):
        values = options[key]
        if not isinstance(values, dict) or any(not isinstance(name, str) or not isinstance(reason, str) or not reason.strip() for name, reason in values.items()):
            raise PortfolioError(f"validation.{key} requires names and non-empty reasons")
    if not isinstance(options["suites"], list):
        raise PortfolioError("validation.suites must be a list")
    for suite in options["suites"]:
        if not isinstance(suite, dict) or set(suite) != {"name", "command", "reports"} or not isinstance(suite["name"], str) or not re.fullmatch(r"[A-Za-z0-9_-]+", suite["name"]) or suite["name"] == "unit":
            raise PortfolioError("custom suites require a unique name, command, and reports")
        validate_commands([suite["command"]])
        if not isinstance(suite["reports"], list) or not suite["reports"] or any(not isinstance(path, str) or invalid_relative_path(path) for path in suite["reports"]):
            raise PortfolioError("suite reports must be relative report globs")
    if len({suite["name"] for suite in options["suites"]}) != len(options["suites"]):
        raise PortfolioError("duplicate validation suite name")


def invalid_relative_path(path: str) -> bool:
    from pathlib import PurePath
    return not path or PurePath(path).is_absolute() or ".." in PurePath(path).parts


def validate_publishing(options: dict[str, Any]) -> None:
    for key in ("enabled", "independent_applications", "private"):
        if not isinstance(options[key], bool):
            raise PortfolioError(f"workflow.publishing.{key} must be boolean")
    for key, choices in (("provider", {"github", "gitlab"}), ("mode", {"autocreate", "precreated"}),
                         ("history", {"default", "all"})):
        if options[key] not in choices:
            raise PortfolioError(f"publishing.{key} must be one of {', '.join(sorted(choices))}")
    for key in ("owner", "prefix", "default_branch", "source_branch", "name"):
        if key in options and not isinstance(options[key], str):
            raise PortfolioError(f"publishing.{key} must be a string")
    if not options["default_branch"] or not options["source_branch"]:
        raise PortfolioError("publishing branch names must be non-empty")
    for key in ("api_url", "url", "token_env", "source_default_branch"):
        value = options.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise PortfolioError(f"publishing.{key} must be a non-empty string or null")
        if key in {"api_url", "url"} and value:
            validate_location(value)
    if options.get("token_env") and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", options["token_env"]):
        raise PortfolioError("publishing.token_env must be an environment variable name")
    if options["namespace_id"] is not None and (type(options["namespace_id"]) is not int or options["namespace_id"] < 1):
        raise PortfolioError("publishing.namespace_id must be a positive integer or null")
    request = options["request"]
    if not isinstance(request, dict) or set(request) != {"enabled", "base", "links"} or type(request["enabled"]) is not bool:
        raise PortfolioError("publishing.request requires enabled, base, and links")
    if request["base"] is not None and (not isinstance(request["base"], str) or not request["base"]):
        raise PortfolioError("publishing.request.base must be a branch name or null")
    if not isinstance(request["links"], list) or any(not isinstance(link, str) or not link.startswith("https://") for link in request["links"]):
        raise PortfolioError("publishing.request.links must contain HTTPS URLs")
    for link in request["links"]:
        validate_location(link)


def validate_location(value: str) -> None:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme in {"http", "https"} and (parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise PortfolioError("repository/API URLs must not contain credentials, query strings, or fragments; use credential helpers or token_env")


def compatibility_hash(config: dict[str, Any], workflow: dict[str, Any]) -> str:
    from .runs import canonical
    return canonical({key: config.get(key) for key in ("targets", "alignment", "migration")} |
                     {"validation": workflow["validation"]})


def merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in updates.items():
        if key not in base:
            raise PortfolioError(f"unknown workflow option: {key}")
        if isinstance(base[key], dict) and key not in {"repositories", "dependency_evidence", "source_credentials", "exclusions", "test_exemptions"}:
            if not isinstance(value, dict):
                raise PortfolioError(f"workflow.{key} must be a mapping")
            result[key] = merge(base[key], value)
        else:
            result[key] = value
    return result


def workflow_policy(config: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = config.get("workflow", {})
    if not isinstance(raw, dict):
        raise PortfolioError("workflow must be a mapping")
    result = merge(DEFAULTS, raw)
    if overrides:
        result = merge(result, overrides)
    if result["mode"] not in {"manual", "unattended"}:
        raise PortfolioError("workflow.mode must be manual or unattended")
    if result["source_selection"] not in {"default", "current"}:
        raise PortfolioError("workflow.source_selection must be default or current")
    if not isinstance(result["source_credentials"], dict) or any(not isinstance(host, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name)) for host, name in result["source_credentials"].items()):
        raise PortfolioError("source_credentials must map intended hosts to token environment names")
    for key, value in result["diagnostics"].items():
        if type(value) is not int or value < 1:
            raise PortfolioError(f"diagnostics.{key} must be a positive integer")
    if type(result["retention"]["days"]) is not int or result["retention"]["days"] < 1 or not isinstance(result["retention"]["pins"], list) or any(not isinstance(pin, str) or not pin for pin in result["retention"]["pins"]):
        raise PortfolioError("retention requires positive days and a list of run/evidence pins")
    for key in ("show_diffs", "include_dependencies", "allow_dependency_override", "allow_downgrades"):
        if not isinstance(result[key], bool):
            raise PortfolioError(f"workflow.{key} must be boolean")
    checkpoints = result["checkpoints"]
    if not isinstance(checkpoints, list) or any(item not in STAGES for item in checkpoints):
        raise PortfolioError("workflow.checkpoints must be a list of stage names")
    if result["mode"] == "unattended" and result["allow_dependency_override"]:
        raise PortfolioError("dependency overrides are allowed only in manual mode")
    if not isinstance(result["dependency_evidence"], dict):
        raise PortfolioError("workflow.dependency_evidence must be a mapping")
    if any(not isinstance(key, str) or not isinstance(value, str) or not value for key, value in result["dependency_evidence"].items()):
        raise PortfolioError("dependency_evidence must map repository keys to evidence paths")
    validation = result["validation"]
    if validation["build"] not in {"compile", "test"}:
        raise PortfolioError("workflow.validation.build must be compile or test")
    if type(validation["timeout"]) is not int or validation["timeout"] < 1:
        raise PortfolioError("workflow.validation.timeout must be positive")
    validate_commands(validation["commands"])
    validate_validation(validation)
    if not isinstance(validation["repositories"], dict):
        raise PortfolioError("workflow.validation.repositories must be a repository-key mapping")
    for value in validation["repositories"].values():
        if not isinstance(value, dict) or set(value) - (set(validation) - {"repositories"}):
            raise PortfolioError("unknown repository validation option")
        validate_validation({**validation, **value})
        validate_commands(value.get("commands", []))
    publishing = result["publishing"]
    validate_publishing(publishing)
    if not isinstance(publishing["targets"], list) or not publishing["targets"] or any(
        target not in {"src_repo", "local_repo", "dst_repo"} for target in publishing["targets"]
    ):
        raise PortfolioError("publishing.targets must select src_repo, local_repo, or dst_repo")
    if not isinstance(publishing["repositories"], dict):
        raise PortfolioError("publishing.repositories must be a repository-key mapping")
    allowed = set(publishing) - {"repositories", "enabled", "targets", "history", "independent_applications"}
    allowed |= {"name", "url"}
    for value in publishing["repositories"].values():
        if not isinstance(value, dict) or set(value) - allowed:
            raise PortfolioError("unknown repository publishing option")
        validate_publishing({**publishing, **value, "request": {**publishing["request"], **value.get("request", {})}})
    pins = config.get("alignment", {}).get("dependencies", {}).get("pins", {})
    if not isinstance(pins, dict) or any(not isinstance(key, str) or ":" not in key or not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value) for key, value in pins.items()):
        raise PortfolioError("alignment.dependencies.pins must map group:artifact patterns to exact versions")
    return result


def resolve_pin(coordinate: str, pins: dict[str, Any]) -> str | None:
    if coordinate in pins:
        return str(pins[coordinate])
    matches = {str(version) for pattern, version in pins.items() if fnmatch.fnmatchcase(coordinate, pattern)}
    if len(matches) > 1:
        raise PortfolioError(f"conflicting wildcard pins for {coordinate}: {', '.join(sorted(matches))}")
    return next(iter(matches), None)


def numeric_version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value.split("-", 1)[0]))


def target_recipes(config: dict[str, Any], framework: bool = True) -> list[str]:
    java = str(config["targets"]["java"]["desired"])
    if java not in {"11", "17", "21", "25"}:
        raise PortfolioError(f"unsupported Java target: {java}")
    boot = str(config["targets"]["spring_boot"]["desired"])
    line = ".".join(boot.split(".")[:2])
    supported = {"3.5": "org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_5",
                 "4.0": "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"}
    rewrite = config.get("migration", {}).get("openrewrite", {})
    mappings = rewrite.get("target_recipes", {})
    if not isinstance(mappings, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in mappings.items()):
        raise PortfolioError("migration.openrewrite.target_recipes must map target lines to recipe names")
    recipe = mappings.get(boot) or mappings.get(line) or supported.get(line)
    if not recipe or not isinstance(recipe, str):
        raise PortfolioError(f"unsupported Spring Boot target: {boot}; configure target_recipes explicitly")
    if framework and int(java) < 17 and int(boot.split(".")[0]) >= 3:
        raise PortfolioError(f"Spring Boot {boot} requires a Java target of at least 17")
    explicit = rewrite.get("recipes", [])
    if not isinstance(explicit, list) or not all(isinstance(item, str) for item in explicit):
        raise PortfolioError("migration.openrewrite.recipes must be a list of strings")
    for item in explicit:
        match = re.search(r"UpgradeSpringBoot_(\d+)_(\d+)", item)
        if match and ".".join(match.groups()) != line:
            raise PortfolioError(f"recipe {item} contradicts Spring Boot target {boot}")
    return list(dict.fromkeys([recipe, *explicit]))
