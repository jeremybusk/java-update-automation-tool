"""Validation of migrated checkouts and effective build-model inventories."""
from __future__ import annotations

import dataclasses
import json
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Sequence

import java_migrator as engine
from .core import Repository, Portfolio, PortfolioError, STAGES, artifact_path, assess, discover_repository, now, read_json, version_matches, write_json
from .policy import resolve_pin, compatibility_hash
from .runs import command, files_digest, git, IGNORED


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
    java, boot, dependencies = set(), set(), []
    for project in projects:
        props = child(project, "properties")
        for key in ("maven.compiler.release", "java.version", "maven.compiler.target", "maven.compiler.source"):
            value = content(props, key)
            if value:
                java.add(value.removeprefix("1."))
                break
        build = child(project, "build")
        plugins = child(build, "plugins") if build is not None else None
        for plugin in list(plugins) if plugins is not None else []:
            if content(plugin, "artifactId") == "maven-compiler-plugin":
                config = child(plugin, "configuration")
                value = content(config, "release") or content(config, "target") or content(config, "source")
                if value:
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
    return {"java_versions": sorted(java), "spring_boot_versions": sorted(boot), "dependencies": dependencies}


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


def inventory(build: engine.BuildRoot, directory: Path, timeout: int) -> dict[str, Any]:
    executable = engine.executable(build)
    if build.tool == "maven":
        path = directory / "effective-pom.xml"
        command([executable, "-B", "help:effective-pom", f"-Doutput={path}"], build.path, timeout)
        values = maven_inventory(path)
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
                  "generated_at": now(), "repository": repo.key, "status": "failed", "checks": []}
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
            roots = engine.discover_builds(output, "auto", config.get("discovery", {}).get("max_depth", 5))
            if not roots:
                raise PortfolioError("no supported builds found in migrated output")
            options = workflow["validation"]
            java, boot, dependencies = set(), set(), []
            for index, build in enumerate(roots):
                build_directory = directory / f"build-{index}"
                build_directory.mkdir(exist_ok=True)
                executable = engine.executable(build)
                argv = ([executable, "-B", "test" if options["build"] == "test" else "compile"]
                        if build.tool == "maven" else [executable, "--no-daemon", "test" if options["build"] == "test" else "classes"])
                command(argv, build.path, options["timeout"])
                result["checks"].append({"name": f"build-{index}", "status": "passed", "command": argv})
                values = inventory(build, build_directory, options["timeout"])
                java.update(values["java_versions"])
                boot.update(values["spring_boot_versions"])
                dependencies.extend(values["dependencies"])
            for argv in options["commands"] + options["repositories"].get(repo.key, {}).get("commands", []):
                command(argv, output, options["timeout"])
                result["checks"].append({"name": "custom", "status": "passed", "command": argv})
            if initial_digest != files_digest(output):
                raise PortfolioError("validation commands modified source; commit changes and rerun validation")
            receipt = read_json(root / "run.json")
            git(output, "merge-base", "--is-ancestor", receipt["sources"][repo.key]["commit"], "HEAD")
            changed_repo = dataclasses.replace(repo, source=str(output), ref=None)
            discovery = discover_repository(changed_repo, config, root, False)
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
                           "inventory": {"java_versions": sorted(java), "spring_boot_versions": sorted(boot), "dependencies": dependencies}})
        except (PortfolioError, OSError, ValueError, KeyError) as exc:
            result["error"] = str(exc)
        write_json(artifact_path(root, STAGES[4], "repositories", repo.key), result)
        results.append(result)
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
