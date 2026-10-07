"""Validation of migrated checkouts and effective build-model inventories."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Sequence

import java_migrator as engine
from .core import Repository, Portfolio, PortfolioError, STAGES, artifact_path, assess, discover_repository, now, read_json, version_matches, write_json
from .policy import resolve_pin, compatibility_hash
from .runs import command, files_digest, git, IGNORED
from .operations import event


def child(node: ET.Element, name: str) -> ET.Element | None:
    return next((item for item in node if item.tag.rsplit("}", 1)[-1] == name), None)


def content(node: ET.Element | None, name: str) -> str:
    element = child(node, name) if node is not None else None
    return element.text.strip() if element is not None and element.text else ""


def maven_inventory(path: Path) -> dict[str, Any]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise PortfolioError("effective Maven model was not produced or is invalid") from exc
    projects = [root] if root.tag.rsplit("}", 1)[-1] == "project" else [item for item in root if item.tag.rsplit("}", 1)[-1] == "project"]
    java, boot, dependencies, metadata = set(), set(), [], []
    for project in projects:
        props = child(project, "properties")
        metadata.append({"project": content(project, "artifactId"), "java.version": content(props, "java.version")})
        build = child(project, "build")
        plugins = child(build, "plugins") if build is not None else None
        compiler = next((plugin for plugin in list(plugins) if content(plugin, "artifactId") == "maven-compiler-plugin"), None) if plugins is not None else None
        config = child(compiler, "configuration") if compiler is not None else None
        def setting(configuration, name):
            value = content(configuration, name) or content(config, name) or content(props, "maven.compiler." + name)
            if value.startswith("${") and value.endswith("}"):
                value = content(props, value[2:-1])
            return value
        executions = child(compiler, "executions") if compiler is not None else None
        configurations = []
        default_goals = set()
        for execution in list(executions) if executions is not None else []:
            goals = child(execution, "goals")
            if goals is not None and any((goal.text or "") in {"compile", "testCompile"} for goal in goals):
                configurations.append(child(execution, "configuration"))
                if content(execution, "id") in {"default-compile", "default-testCompile"}:
                    default_goals.update((goal.text or "") for goal in goals)
        if not {"compile", "testCompile"}.issubset(default_goals):
            configurations.append(config)
        if content(project, "packaging") != "pom":
            for configuration in configurations:
                value = setting(configuration, "release") or setting(configuration, "target") or setting(configuration, "source")
                if not value or "${" in value:
                    raise PortfolioError("effective Maven compiler level is unresolved; configure compiler release/target")
                java.add(value.removeprefix("1."))
        declared = child(project, "dependencies")
        for dependency in list(declared) if declared is not None else []:
            group, artifact, version = (content(dependency, key) for key in ("groupId", "artifactId", "version"))
            if not group or not artifact or not version or "${" in version:
                raise PortfolioError("effective Maven dependency has an unresolved coordinate/version")
            dependencies.append({"coordinate": f"{group}:{artifact}", "version": version,
                                 "group": group, "artifact": artifact})
            if group == "org.springframework.boot":
                boot.add(version)
        parent = child(project, "parent")
        if content(parent, "groupId") == "org.springframework.boot":
            boot.add(content(parent, "version"))
    return {"java_versions": sorted(java), "spring_boot_versions": sorted(boot), "dependencies": dependencies, "compiler_metadata": metadata}


def gradle_script(destination: Path) -> str:
    # Resolve the build's actual models, including BOMs and version catalogs.
    return '''import groovy.json.JsonOutput
gradle.projectsEvaluated {
  rootProject.tasks.register('javaUpdateInventory') {
    doLast {
      def dependencies = []
      def javaVersions = []
      rootProject.allprojects.each { p ->
        def compilers = p.tasks.withType(org.gradle.api.tasks.compile.JavaCompile)
        compilers.each { task ->
          javaVersions << (task.options.release.present ? task.options.release.get().toString() : task.targetCompatibility.toString().replaceFirst('^1\\\\.', ''))
        }
        if (compilers.isEmpty()) {
          def javaExt = p.extensions.findByName('java')
          if (javaExt != null) {
            def level = javaExt.toolchain.languageVersion.orNull
            javaVersions << (level != null ? level.toString() : javaExt.targetCompatibility.toString().replaceFirst('^1\\\\.', ''))
          }
        }
        p.configurations.findAll { it.canBeResolved }.each { configuration ->
          def resolved = configuration.resolvedConfiguration
          resolved.rethrowFailure()
          resolved.resolvedArtifacts.each { artifact ->
            def id = artifact.moduleVersion.id
            dependencies << [coordinate: "${id.group}:${id.name}".toString(), group: id.group, artifact: id.name, version: id.version]
          }
        }
      }
      def inventory = [dependencies: dependencies.unique(), java_versions: javaVersions.unique().sort(), spring_boot_versions: dependencies.findAll { it.group == 'org.springframework.boot' }.collect { it.version }.unique().sort()]
      new File(OUTPUT_FILE).text = JsonOutput.toJson(inventory)
    }
  }
}
'''.replace("OUTPUT_FILE", json.dumps(str(destination)))


def inventory(build: engine.BuildRoot, directory: Path, timeout: int, effective_pom: Path | None = None) -> dict[str, Any]:
    executable = engine.executable(build)
    if build.tool == "maven":
        path = effective_pom or directory / "effective-pom.xml"
        if effective_pom is None:
            command([executable, "-B", "help:effective-pom", f"-Doutput={path}"], build.path, timeout)
        values = maven_inventory(path)
        for stale in build.path.rglob("target/java-update-dependencies.tgf"):
            stale.unlink()
        command([executable, "-B", "dependency:tree", "-DoutputType=tgf",
                 "-DoutputFile=target/java-update-dependencies.tgf"], build.path, timeout)
        trees = list(build.path.rglob("target/java-update-dependencies.tgf"))
        if not trees:
            raise PortfolioError("resolved Maven dependency trees were not produced")
        for index, tree in enumerate(trees):
            text = tree.read_text()
            (directory / f"resolved-{index}.tgf").write_text(text)
            nodes = text.split("#", 1)[0].strip().splitlines()
            for line in nodes[1:]:
                parts = line.split(maxsplit=1)
                if len(parts) != 2:
                    raise PortfolioError("invalid resolved Maven dependency tree")
                gav = parts[1].split(":")
                if len(gav) < 4:
                    raise PortfolioError("unresolved Maven dependency coordinate")
                version = gav[-2] if gav[-1] in {"compile", "test", "runtime", "provided", "system", "import"} else gav[-1]
                if not version or "${" in version:
                    raise PortfolioError("unresolved Maven dependency version")
                values["dependencies"].append({"coordinate": f"{gav[0]}:{gav[1]}", "version": version, "group": gav[0], "artifact": gav[1]})
                if gav[0] == "org.springframework.boot":
                    values["spring_boot_versions"] = sorted(set(values["spring_boot_versions"]) | {version})
        return values
    path = directory / "effective-gradle.json"
    init = directory / "inventory.gradle"
    init.write_text(gradle_script(path))
    command([executable, "--no-daemon", "--init-script", str(init), "javaUpdateInventory"], build.path, timeout)
    return read_json(path)


def report_paths(root: Path, patterns: list[str]) -> list[Path]:
    paths = sorted({path for pattern in patterns for path in root.glob(pattern) if path.is_file()})
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise PortfolioError("test report points outside the validated build")
    return paths


def clear_reports(root: Path, patterns: list[str]) -> None:
    tracked = set(git(root, "ls-files", "-z").split("\0"))
    for path in report_paths(root, patterns):
        if str(path.relative_to(root)) in tracked:
            raise PortfolioError("test reports must be generated output, not tracked source")
        path.unlink()


def test_evidence(root: Path, patterns: list[str], suite: str, exemption: str | None, directory: Path) -> dict[str, Any]:
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    reports = []
    for index, path in enumerate(report_paths(root, patterns)):
        try:
            model = ET.parse(path).getroot()
            suites = [model] if model.tag == "testsuite" else list(model.iter("testsuite"))
            if not suites:
                raise ValueError("no test suites")
            for item in suites:
                values = {key: int(item.get(key, "0")) for key in counts}
                if any(value < 0 for value in values.values()) or values["skipped"] > values["tests"]:
                    raise ValueError("invalid test counts")
                for key in counts:
                    counts[key] += values[key]
        except (ValueError, ET.ParseError) as exc:
            raise PortfolioError(f"invalid fresh test report: {suite}") from exc
        data = path.read_bytes()
        destination = directory / f"{suite}-{index}.xml"
        destination.write_bytes(data)
        reports.append({"source": str(path.relative_to(root)), "resource": str(destination), "sha256": hashlib.sha256(data).hexdigest()})
    if counts["failures"] or counts["errors"]:
        raise PortfolioError(f"{suite} test reports contain failures/errors")
    executed = counts["tests"] - counts["skipped"]
    if executed <= 0 and not exemption:
        raise PortfolioError(f"{suite} requires fresh executed tests; missing, empty, or entirely skipped reports need a reasoned exemption")
    return {"suite": suite, "status": "executed" if executed > 0 else "exempted", "reason": exemption if executed <= 0 else None,
            "counts": counts, "reports": reports, "fresh": True}


def verify_validation_evidence(record: dict[str, Any]) -> None:
    if record.get("evidence_version") != 2 or not record.get("test_evidence"):
        raise PortfolioError("validation lacks fresh test evidence; rerun validation")
    resources = list(record.get("inventory_resources", []))
    for proof in record["test_evidence"]:
        resources.extend(proof["reports"])
    for resource in resources:
        path = Path(resource["resource"])
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != resource["sha256"]:
            raise PortfolioError("retained validation evidence changed or is missing; rerun validation")


def declared_integration(build: engine.BuildRoot, effective_pom: Path | None = None) -> dict[str, Any]:
    import re
    suites, tasks = {}, []
    if build.tool == "maven":
        for path in [effective_pom] if effective_pom else build.path.rglob("pom.xml"):
            if effective_pom:
                model = ET.parse(path).getroot()
                projects = [model] if model.tag.rsplit("}", 1)[-1] == "project" else list(model)
            else:
                if any(part in {"target", ".git"} for part in path.relative_to(build.path).parts):
                    continue
                projects = [ET.parse(path).getroot()]
            for model in projects:
                configured = child(model, "build")
                plugins = child(configured, "plugins") if configured is not None else None
                if plugins is not None and any(content(plugin, "artifactId") == "maven-failsafe-plugin" for plugin in plugins):
                    suites["integration"] = ["**/target/failsafe-reports/TEST-*.xml"]
    else:
        for path in [*build.path.rglob("build.gradle"), *build.path.rglob("build.gradle.kts")]:
            if any(part in {"build", ".git"} for part in path.relative_to(build.path).parts):
                continue
            text = path.read_text()
            for name in re.findall(r"(?:register|create|named)\s*[<(][^\n]*?['\"]([^'\"]+)['\"]", text):
                if name != "test" and re.search(r"integration|contract|functional", name, re.I):
                    tasks.append(name)
                    suites[name] = [f"**/build/test-results/{name}/TEST-*.xml"]
            for name in re.findall(r"(?:^|\n)\s*(integrationTest|contractTest|functionalTest)\s*[({]", text):
                tasks.append(name)
                suites[name] = [f"**/build/test-results/{name}/TEST-*.xml"]
    return {"tasks": sorted(set(tasks)), "suites": suites, "reports": [pattern for patterns in suites.values() for pattern in patterns]}


def _validate_build(build: engine.BuildRoot, build_root: str, directory: Path,
                    options: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Run one build and retain each check before execution, including failures."""
    directory.mkdir(exist_ok=True)
    executable = engine.executable(build)
    timeout = options["timeout"]
    result["toolchains"].append({
        "build_root": build_root, "executable": executable,
        "version": command([executable, "--version"], build.path, timeout),
    })
    maven = build.tool == "maven"
    args = [executable, "-B"] if maven else [executable, "--no-daemon", "--rerun-tasks", "--no-build-cache"]
    unit = ["**/target/surefire-reports/TEST-*.xml"] if maven else ["**/build/test-results/test/TEST-*.xml"]
    effective_pom = None
    if maven:
        effective_pom = directory / "effective-pom.xml"
        command([executable, "-B", "help:effective-pom", f"-Doutput={effective_pom}"], build.path, timeout)
    integration = declared_integration(build, effective_pom)
    reports = unit + integration["reports"]
    clear_reports(build.path, reports)
    if maven and integration["reports"]:
        task = "verify"
    elif options["build"] == "test":
        task = "test"
    else:
        task = "compile" if maven else "classes"
    argv = [*args, task]
    if not maven and integration["tasks"]:
        argv += integration["tasks"]
    check = {"name": directory.name, "build_root": build_root, "status": "failed", "command": argv}
    result["checks"].append(check)
    command(argv, build.path, timeout)
    check["status"] = "passed"
    suites = {"unit": unit, **integration["suites"]}
    for name, globs in suites.items():
        proof = test_evidence(build.path, globs, name, options["test_exemptions"].get(name), directory)
        proof.update(build_root=build_root, command=argv)
        result["test_evidence"].append(proof)
    for suite in options["suites"]:
        name = suite["name"]
        clear_reports(build.path, suite["reports"])
        check = {"name": name, "status": "failed", "command": suite["command"]}
        result["checks"].append(check)
        command(suite["command"], build.path, timeout)
        custom_directory = directory / "custom"
        custom_directory.mkdir(exist_ok=True)
        proof = test_evidence(build.path, suite["reports"], name,
                              options["test_exemptions"].get(name), custom_directory)
        proof.update(build_root=build_root, command=suite["command"])
        result["test_evidence"].append(proof)
        check["status"] = "passed"
    return inventory(build, directory, timeout, effective_pom)


def validate_stage(selected: Sequence[Repository], portfolio: Portfolio, config: dict[str, Any], workflow: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    results, discoveries = [], []
    checkouts = {}
    validation_state = root / STAGES[4] / "assessment"
    history = root / STAGES[4] / "history" / uuid.uuid4().hex[:8]
    for directory in (validation_state, root / STAGES[4] / "applications", root / STAGES[4] / "application-groups"):
        if directory.exists():
            history.mkdir(parents=True, exist_ok=True)
            directory.rename(history / directory.name)
    for repo in selected:
        directory = artifact_path(root, STAGES[4], "repositories", repo.key).parent
        directory.mkdir(parents=True, exist_ok=True)
        result = {"schema_version": 1, "artifact_type": "repository-validation-result", "stage": STAGES[4],
                  "generated_at": now(), "repository": repo.key, "status": "failed", "checks": [],
                  "diagnostics": str(root / "diagnostics"), "journal": str(root / "events.jsonl"), "toolchains": []}
        try:
            migration = read_json(artifact_path(root, STAGES[3], "repositories", repo.key))
            if migration["status"] != "migrated":
                raise PortfolioError("migration did not produce a successful migrated checkout")
            output = Path(migration["output"])
            git(output, "diff", "--exit-code", "HEAD")
            untracked = git(output, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
            if any(path and not any(part in IGNORED for part in Path(path).parts) for path in untracked):
                raise PortfolioError("migrated output contains uncommitted source files")
            initial_digest = files_digest(output)
            memberships = []
            roots = engine.discover_builds(output, "auto", config.get("discovery", {}).get("max_depth", 5), memberships=memberships)
            if not roots:
                raise PortfolioError("no supported builds found in migrated output")
            options = {**workflow["validation"], **workflow["validation"]["repositories"].get(repo.key, {})}
            names = {str(build.path.relative_to(output)) for build in roots}
            unknown = (set(options["build_roots"]) | set(options["exclusions"])) - names
            if unknown:
                raise PortfolioError(f"unknown independent build roots: {sorted(unknown)}")
            exclusions = dict(options["exclusions"])
            if options["build_roots"]:
                for name in names - set(options["build_roots"]):
                    if name not in exclusions:
                        raise PortfolioError(f"unselected build requires an exclusion reason: {name}")
            required = [build for build in roots if str(build.path.relative_to(output)) not in exclusions]
            if not required:
                raise PortfolioError("validation must include at least one independent build")
            result["scope"] = {"included": [str(build.path.relative_to(output)) for build in required],
                               "excluded": exclusions, "coverage": "selected-builds" if exclusions else "discovered-builds",
                               "memberships": memberships}
            result["test_evidence"] = []
            java, boot, dependencies, compiler_metadata = set(), set(), [], []
            inventory_resources = []
            for index, build in enumerate(required):
                build_directory = directory / f"build-{index}"
                build_root = str(build.path.relative_to(output))
                values = _validate_build(build, build_root, build_directory, options, result)
                java.update(values["java_versions"])
                boot.update(values["spring_boot_versions"])
                dependencies.extend(values["dependencies"])
                compiler_metadata.extend({"build_root": build_root, **item}
                                         for item in values.get("compiler_metadata", []))
                inventory_resources.extend({"resource": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                    for path in build_directory.iterdir() if path.name in {"effective-pom.xml", "effective-gradle.json"} or path.suffix == ".tgf")
            commands = workflow["validation"]["commands"] + workflow["validation"]["repositories"].get(repo.key, {}).get("commands", [])
            for argv in commands:
                check = {"name": "custom", "status": "failed", "command": argv}
                result["checks"].append(check)
                command(argv, output, options["timeout"])
                check["status"] = "passed"
            if initial_digest != files_digest(output):
                raise PortfolioError("validation commands modified source; commit changes and rerun validation")
            receipt = read_json(root / "run.json")
            git(output, "merge-base", "--is-ancestor", receipt["sources"][repo.key]["commit"], "HEAD")
            changed_repo = dataclasses.replace(repo, source=str(output), ref=None)
            discovery = discover_repository(changed_repo, config)
            discovery["projects"] = [project for project in discovery["projects"] if not any(
                Path(project.get("path", ".")).is_relative_to(Path(excluded)) for excluded in exclusions)]
            discovery["dependencies"] = dependencies
            discovery["summary"]["java_versions"] = sorted(java)
            discovery["summary"]["spring_boot_versions"] = sorted(boot)
            pins = config.get("alignment", {}).get("dependencies", {}).get("pins", {})
            for dependency in dependencies:
                version = dependency.get("version", "")
                if not version or version in {"unknown", "unspecified"} or "${" in version:
                    raise PortfolioError("resolved dependency inventory contains an unknown version")
                pinned = resolve_pin(dependency["coordinate"], pins)
                if pinned and dependency["version"] != pinned:
                    raise PortfolioError(f"unmet pin: {dependency['coordinate']} expected {pinned}, found {dependency['version']}")
            for name, versions in (("java", java), ("spring_boot", boot)):
                if name == "spring_boot" and not versions and not any("spring-boot" in project["features"] for project in discovery["projects"]):
                    continue
                target = config["targets"][name]
                if not versions or any(not any(version_matches(value, str(pattern)) for pattern in target["acceptable"]) for value in versions):
                    raise PortfolioError(f"migrated {name} versions are unknown or outside the acceptable policy")
            discoveries.append(discovery)
            checkouts[repo.key] = changed_repo
            result.update({"status": "validated", "output": str(output), "commit": git(output, "rev-parse", "HEAD"),
                           "tree_hash": files_digest(output), "config_hash": receipt["config_hash"],
                           "source_commit": receipt["sources"][repo.key]["commit"],
                           "compatibility_hash": compatibility_hash(config, workflow),
                           "evidence_version": 2,
                           "inventory_resources": inventory_resources,
                           "inventory": {"java_versions": sorted(java), "spring_boot_versions": sorted(boot), "dependencies": dependencies,
                                         "compiler_metadata": compiler_metadata}})
        except (PortfolioError, engine.MigrationError, ET.ParseError, OSError, ValueError, KeyError) as exc:
            result["error"] = str(exc)
        write_json(artifact_path(root, STAGES[4], "repositories", repo.key), result)
        results.append(result)
        event("validation-finished", repository=repo.key, status=result["status"], checks=result["checks"])
    # Store fresh assessment separately from the reviewed original assessment.
    if discoveries:
        members = [checkouts[repo.key] for repo in selected if repo.key in checkouts]
        assessment_results = assess(members, portfolio, discoveries, config, validation_state)
        for assessment in assessment_results:
            if "cohort_complete" in assessment:
                results.append(assessment)
                kind = "application-groups" if assessment["artifact_type"].startswith("application-group") else "applications"
                write_json(artifact_path(root, STAGES[4], kind, assessment["id"]), assessment)
        for repo, result in zip(selected, results[:len(selected)]):
            if result["status"] != "validated":
                continue
            assessment = read_json(artifact_path(validation_state, STAGES[1], "repositories", repo.key))
            if assessment["dependency_mismatches"] or any(
                item["status"] not in {"desired", "acceptable", "not-applicable"} for item in assessment["versions"].values()
            ):
                result.update({"status": "failed", "error": "fresh repository assessment is not compliant"})
                write_json(artifact_path(root, STAGES[4], "repositories", repo.key), result)
    return results
