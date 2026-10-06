"""Build pinned recipe sources into a private, reusable Maven repository."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = ROOT / "recipe-sources.lock.json"
DEFAULT_CACHE = Path.home() / ".cache/java-update/recipes"
CODE_GENOME = "https://artifacts.codegenomeproject.org/maven"


class SourceBuildError(RuntimeError):
    pass


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_order(lock: dict, artifacts: list[str]) -> list[dict]:
    modules = {item["artifact"].split(":")[1]: item for item in lock["modules"]}
    by_coordinate = {item["artifact"]: item for item in lock["modules"]}
    ordered, visiting, visited = [], set(), set()

    def visit(name):
        if name in visiting:
            raise SourceBuildError(f"cycle in recipe source lock: {name}")
        if name in visited:
            return
        if name not in modules:
            raise SourceBuildError(f"missing source dependency in lock: {name}")
        visiting.add(name)
        for dependency in modules[name]["depends_on"]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
        ordered.append(modules[name])

    for artifact in artifacts:
        if artifact not in by_coordinate:
            raise SourceBuildError(f"recipe source lock has no entry for {artifact}; update the lock or select a binary repository")
        visit(artifact.split(":")[1])
    return ordered


def cache_valid(receipt: Path, repository: Path) -> bool:
    try:
        files = json.loads(receipt.read_text())["files"]
        return bool(files) and all(digest(repository / name) == expected for name, expected in files.items())
    except (OSError, ValueError, KeyError):
        return False


def validate_pom(path: Path, lock: dict) -> None:
    namespace = {"p": "http://maven.apache.org/POM/4.0.0"}
    for dependency in ET.parse(path).findall("p:dependencies/p:dependency", namespace):
        group = dependency.findtext("p:groupId", namespaces=namespace)
        artifact = dependency.findtext("p:artifactId", namespaces=namespace)
        expected = lock["dependency_versions"].get(f"{group}:{artifact}")
        actual = dependency.findtext("p:version", namespaces=namespace)
        if expected and actual != expected:
            raise SourceBuildError(f"published dependency {group}:{artifact} is {actual}, expected {expected}; see {path}")


def settings_text(name: str, lock: dict, projects: list[str] = ()) -> str:
    return f'''pluginManagement {{
    repositories {{
        maven {{
            url = uri(System.getenv("JAVA_UPDATE_BUILD_REPOSITORY"))
            if (System.getenv("JAVA_UPDATE_BUILD_USERNAME") && System.getenv("JAVA_UPDATE_BUILD_TOKEN")) {{
                credentials {{ username = System.getenv("JAVA_UPDATE_BUILD_USERNAME"); password = System.getenv("JAVA_UPDATE_BUILD_TOKEN") }}
            }}
            if (System.getenv("JAVA_UPDATE_BUILD_REPOSITORY") == {json.dumps(CODE_GENOME)}) {{
                content {{ includeGroupByRegex("org\\\\.openrewrite.*"); includeGroupByRegex("io\\\\.moderne.*") }}
            }}
        }}
        gradlePluginPortal()
        mavenCentral()
    }}
    resolutionStrategy.eachPlugin {{
        if (requested.id.id.startsWith("org.openrewrite.build.")) useVersion({json.dumps(lock["build_plugin_version"])})
    }}
}}
rootProject.name = {json.dumps(name)}
{''.join('include(' + json.dumps(project) + ')\n' for project in projects)}
'''


def init_text(lock: dict, module: dict, repository: Path) -> str:
    versions = json.dumps(lock["dependency_versions"])
    version = module["artifact"].split(":")[2]
    build_dependencies = json.dumps(module.get("build_dependencies", []))
    return f'''def pinnedVersions = new groovy.json.JsonSlurper().parseText({json.dumps(versions)})
allprojects {{ project ->
    repositories {{
        mavenLocal {{ url = uri({json.dumps(repository.as_uri())}) }}
        maven {{
            url = uri(System.getenv("JAVA_UPDATE_BUILD_REPOSITORY"))
            if (System.getenv("JAVA_UPDATE_BUILD_USERNAME") && System.getenv("JAVA_UPDATE_BUILD_TOKEN")) {{
                credentials {{ username = System.getenv("JAVA_UPDATE_BUILD_USERNAME"); password = System.getenv("JAVA_UPDATE_BUILD_TOKEN") }}
            }}
            if (System.getenv("JAVA_UPDATE_BUILD_REPOSITORY") == {json.dumps(CODE_GENOME)}) {{
                content {{ includeGroupByRegex("org\\\\.openrewrite.*"); includeGroupByRegex("io\\\\.moderne.*") }}
            }}
        }}
        mavenCentral()
    }}
    configurations.configureEach {{
        resolutionStrategy.eachDependency {{ details ->
            def key = details.requested.group + ':' + details.requested.name
            if (pinnedVersions.containsKey(key)) details.useVersion(pinnedVersions[key])
            else if (details.requested.group == 'org.openrewrite' && details.requested.name.startsWith('rewrite-')) details.useVersion({json.dumps(lock["rewrite_version"])})
        }}
    }}
    pluginManager.withPlugin('org.openrewrite.build.recipe-library') {{
        extensions.getByName('rewriteRecipe').rewriteVersion.set({json.dumps(lock["rewrite_version"])})
    }}
    tasks.withType(org.gradle.plugins.signing.Sign).configureEach {{ enabled = false }}
    tasks.withType(Jar).configureEach {{
        from(project.file('JAVA-UPDATE-SOURCE-BUILD.txt')) {{ into('META-INF') }}
        from(project.file('LICENSE.md')) {{ into('META-INF/licenses/' + project.name) }}
        from(project.file('LICENSE')) {{ into('META-INF/licenses/' + project.name) }}
        from(project.file('NOTICE')) {{ into('META-INF/licenses/' + project.name) }}
        if (project != rootProject) {{
            from(rootProject.file('JAVA-UPDATE-SOURCE-BUILD.txt')) {{ into('META-INF') }}
            from(rootProject.file('LICENSE')) {{ into('META-INF/licenses/' + project.name) }}
        }}
    }}
    afterEvaluate {{
        project.version = {json.dumps(version)}
        // Upstream test toolchains may request JDK 25; packaging targets Java 8.
        def javaExtension = extensions.findByName('java')
        if (javaExtension != null) javaExtension.toolchain.languageVersion.set(JavaLanguageVersion.of({lock["java"]}))
        {build_dependencies}.each {{ dependencies.add('compileOnly', it) }}
    }}
}}
'''


def java_api_build_text(lock: dict, module: dict) -> str:
    group, name, version = module["artifact"].split(":")
    dependencies = '\n'.join('    implementation(' + json.dumps(item) + ')' for item in module['compile_dependencies'])
    return f'''plugins {{ id 'java-library'; id 'maven-publish' }}
group = {json.dumps(group)}
version = {json.dumps(version)}
java {{ sourceCompatibility = JavaVersion.VERSION_1_8; targetCompatibility = JavaVersion.VERSION_1_8 }}
dependencies {{
{dependencies}
    compileOnly('org.openrewrite:rewrite-test:{lock["rewrite_version"]}')
    compileOnly('com.google.code.findbugs:jsr305:3.0.2')
    compileOnly('org.projectlombok:lombok:{lock["lombok_version"]}')
    annotationProcessor('org.projectlombok:lombok:{lock["lombok_version"]}')
}}
tasks.withType(JavaCompile).configureEach {{ options.compilerArgs.add('-parameters') }}
publishing {{ publications {{ mavenJava(MavenPublication) {{ from components.java }} }} }}
'''


def fetch_source(module: dict, destination: Path) -> None:
    repository, commit = module["repository"], module["commit"]
    if not re.fullmatch(r"https://github\.com/openrewrite/[a-z0-9-]+", repository) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SourceBuildError("source lock must contain an OpenRewrite GitHub repository and full commit SHA")
    url = repository.replace("https://github.com/", "https://codeload.github.com/") + "/tar.gz/" + commit
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SourceBuildError(f"cannot download source for {module['artifact']}") from exc
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        prefix = members[0].name.split("/")[0]
        for member in members:
            member.name = member.name.removeprefix(prefix + "/")
        archive.extractall(destination, members=[m for m in members if m.name != prefix], filter="data")


def build_module(module: dict, lock: dict, directory: Path, repository: Path, env: dict[str, str]) -> None:
    name = module["artifact"].split(":")[1]
    checkout = directory / "sources" / ("rewrite-" + module["commit"] if module.get("subdirectory") else name)
    if not (checkout / "gradlew").is_file():
        fetch_source(module, checkout)
    source = checkout / module.get("subdirectory", "")
    for patch in module.get('build_patches', []):
        target = source / patch['file']
        backup = target.with_name(target.name + '.java-update-upstream')
        if not backup.exists():
            text = target.read_text()
            if text.count(patch['find']) != 1:
                raise SourceBuildError(f"locked build adaptation does not match {target}")
            backup.write_text(text)
            target.write_text(text.replace(patch['find'], patch['replace']))
    if module.get("license_source"):
        # This older recipe commit only contains a license header. Retain the
        # full agreement from the locked core source, verified by its checksum.
        url = module["license_source"]
        if not re.fullmatch(r"https://raw\.githubusercontent\.com/openrewrite/[a-z0-9-]+/[0-9a-f]{40}/[a-z0-9-]+/LICENSE\.md", url):
            raise SourceBuildError("license source must be an immutable OpenRewrite source URL")
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                license_data = response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SourceBuildError(f"cannot download the locked license for {name}") from exc
        if hashlib.sha256(license_data).hexdigest() != module["license_sha256"]:
            raise SourceBuildError(f"locked license checksum mismatch for {name}")
        (source / "LICENSE.md").write_bytes(license_data)
    if module.get("java_api_only"):
        # Build real Java sources required by compileOnly language checks. Native
        # RPC backends are not used by this Java migration tool.
        for filename in ("build.gradle", "build.gradle.kts"):
            path = source / filename
            backup = source / (filename + ".upstream")
            if path.exists() and not backup.exists():
                path.rename(backup)
        (source / "build.gradle").write_text(java_api_build_text(lock, module))
    # Replace only the build's settings: no public scans or remote build caches.
    for filename in ("settings.gradle", "settings.gradle.kts"):
        path = source / filename
        backup = source / (filename + ".upstream")
        if path.exists() and not backup.exists():
            text = path.read_text()
            if re.search(r"\binclude(?:Build)?\s*\(", text) and not module.get('projects'):
                raise SourceBuildError(f"multi-project source build needs explicit support: {name}")
            path.rename(backup)
    (source / "settings.gradle").write_text(settings_text(name, lock, module.get('projects', [])))
    (source / "JAVA-UPDATE-SOURCE-BUILD.txt").write_text(
        f"Internally built from {module['repository']} at {module['commit']}.\n"
        "Modified build settings and dependency versions; local publication without signing.\n"
        + ("Java-facing API build only; native language RPC backends are not packaged.\n" if module.get("java_api_only") else "") +
        "Upstream source and license notices are retained. This is not a vendor binary.\n")
    init = directory / (name + ".init.gradle")
    init.write_text(init_text(lock, module, repository))
    wrapper = checkout / "gradlew"
    wrapper.chmod(0o755)
    log_path = directory / "logs" / (name + ".log")
    log_path.parent.mkdir(exist_ok=True)
    command = [str(wrapper), "--project-dir", str(source), "--no-daemon", "--no-scan", "--max-workers=2",
               "-Dmaven.repo.local=" + str(repository), "-I", str(init),
               module.get('publish_task', 'publishToMavenLocal'), "-x", "test"]
    if not module.get("java_api_only"):
        command.extend(["-x", "javadoc"])
    secrets = [env.get(key) for key in ("JAVA_UPDATE_BUILD_USERNAME", "JAVA_UPDATE_BUILD_TOKEN") if env.get(key)]
    with log_path.open("w") as log:
        process = subprocess.Popen(command, cwd=source, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        assert process.stdout is not None
        for line in process.stdout:
            for secret in secrets:
                line = line.replace(secret, "[REDACTED]")
            line = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[REDACTED]", line)
            log.write(line)
            log.flush()
        code = process.wait()
    if code:
        raise SourceBuildError(f"source build failed for {module['artifact']} (exit {code}); see {log_path}")
    group, artifact, version = module["artifact"].split(":")
    installed = repository / group.replace(".", "/") / artifact / version
    jar = installed / f"{artifact}-{version}.jar"
    if not jar.is_file() or not (installed / f"{artifact}-{version}.pom").is_file():
        raise SourceBuildError(f"source build did not publish the locked coordinates: {module['artifact']}")
    validate_pom(installed / f"{artifact}-{version}.pom", lock)
    with zipfile.ZipFile(jar) as archive:
        if "META-INF/JAVA-UPDATE-SOURCE-BUILD.txt" not in archive.namelist():
            raise SourceBuildError(f"source build notice missing from {jar}")
        if not any(name.startswith('META-INF/licenses/') and not name.endswith('/') for name in archive.namelist()):
            raise SourceBuildError(f"source license missing from {jar}")
    files = {str(path.relative_to(repository)): digest(path) for path in installed.iterdir() if path.is_file()}
    receipt = directory / "receipts" / (name + ".json")
    receipt.parent.mkdir(exist_ok=True)
    receipt.write_text(json.dumps({"source": module, "files": files}, indent=2) + "\n")


def prepare(artifacts: list[str], *, cache: Path = DEFAULT_CACHE, lock_path: Path = DEFAULT_LOCK,
            remote: str = CODE_GENOME, username_env: str = "CODE_GENOME_USERNAME",
            token_env: str = "CODE_GENOME_TOKEN", env: dict[str, str] | None = None) -> Path:
    env = dict(os.environ if env is None else env)
    if env.get("JAVA_UPDATE_SOURCE_JAVA_HOME"):
        env["JAVA_HOME"] = env["JAVA_UPDATE_SOURCE_JAVA_HOME"]
        env["PATH"] = str(Path(env["JAVA_HOME"]) / "bin") + os.pathsep + env.get("PATH", "")
    lock = json.loads(lock_path.read_text())
    # The published Gradle plugin predates current core APIs; build its upstream
    # compatibility fix with the same core before loading current recipe packs.
    ordered = source_order(lock, ([lock['gradle_plugin_artifact']] if lock.get('gradle_plugin_artifact') else []) + artifacts)
    try:
        java = subprocess.run(["java", "-version"], env=env, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SourceBuildError("source builds need JDK 21 and Java on PATH") from exc
    java_version = java.stdout + java.stderr
    if not re.search(r'version "21[.\"]', java_version):
        raise SourceBuildError("source builds need JDK 21; select JAVA_HOME/PATH before running")
    key = hashlib.sha256((digest(lock_path) + digest(Path(__file__)) + java_version + remote.rstrip('/')).encode()).hexdigest()[:24]
    directory = cache.expanduser().resolve() / key
    repository = directory / "maven"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    repository.mkdir(exist_ok=True)
    env.update({"JAVA_UPDATE_BUILD_REPOSITORY": remote,
                "JAVA_UPDATE_BUILD_USERNAME": env.get(username_env, ""),
                "JAVA_UPDATE_BUILD_TOKEN": env.get(token_env, ""),
                "GRADLE_USER_HOME": str(cache.expanduser().resolve() / "gradle")})
    env.pop("GRADLE_ENTERPRISE_ACCESS_KEY", None)
    # Hosted public CI must disable its Gradle cache upload for source jobs.
    with (directory / "build.lock").open("w") as handle:
        try:
            import fcntl
        except ImportError as exc:
            raise SourceBuildError("source cache locking needs Linux/macOS; use a Linux runner or container") from exc
        fcntl.flock(handle, fcntl.LOCK_EX)
        for module in ordered:
            name = module["artifact"].split(":")[1]
            if cache_valid(directory / "receipts" / (name + ".json"), repository):
                print(f"Recipe source cache: {module['artifact']}", flush=True)
                continue
            if remote.rstrip('/') == CODE_GENOME and not (env["JAVA_UPDATE_BUILD_USERNAME"] and env["JAVA_UPDATE_BUILD_TOKEN"]):
                raise SourceBuildError(f"source build dependencies need a free Code Genome account in {username_env}/{token_env}, or configure an accessible Maven/Nexus artifact_repository")
            print(f"Building recipe source: {module['artifact']}", flush=True)
            build_module(module, lock, directory, repository, env)
    return repository
