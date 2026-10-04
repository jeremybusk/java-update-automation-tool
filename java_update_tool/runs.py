"""Isolated run storage, Git snapshots, and content-bound review checkpoints."""
from __future__ import annotations

import dataclasses
import contextlib
import contextvars
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
import urllib.parse
from pathlib import Path
from typing import Any, Sequence

from .core import Portfolio, PortfolioError, Repository, STAGES, now, read_json, write_json
from .operations import execute, event, lock

IGNORED = {".git", "target", "build", ".gradle", "__pycache__", ".java-update", ".migration-work"}
GIT_CREDENTIALS = contextvars.ContextVar("git_credentials", default=None)


@contextlib.contextmanager
def git_credentials(token: str, url: str):
    marker = GIT_CREDENTIALS.set((token, url))
    try:
        yield
    finally:
        GIT_CREDENTIALS.reset(marker)


def command(argv: Sequence[str], cwd: Path, timeout: int = 300) -> str:
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Java Update Automation",
                "GIT_AUTHOR_EMAIL": "java-update@localhost", "GIT_COMMITTER_NAME": "Java Update Automation",
                "GIT_COMMITTER_EMAIL": "java-update@localhost"})
    try:
        with tempfile.TemporaryDirectory(prefix="java-update-auth-") as temporary:
            credentials = GIT_CREDENTIALS.get()
            if argv[0] == "git" and credentials and credentials[0] and credentials[1] in argv and credentials[1].startswith("https://"):
                askpass = Path(temporary) / "askpass.py"
                askpass.write_text("#!/usr/bin/env python3\nimport os, sys\nprint('oauth2' if 'username' in sys.argv[1].lower() else os.environ['JAVA_UPDATE_GIT_TOKEN'])\n")
                askpass.chmod(0o700)
                env.update({"GIT_ASKPASS": str(askpass), "JAVA_UPDATE_GIT_TOKEN": credentials[0]})
            return execute(list(argv), cwd, env, timeout, require_complete=argv[0] == "git")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PortfolioError(f"command could not finish: {argv[0]}") from exc


def git(path: Path, *argv: str) -> str:
    return command(["git", *argv], path)


def files_digest(root: Path) -> str:
    digest = hashlib.sha256()
    tracked = set()
    if (root / ".git").exists():
        tracked = set(command(["git", "ls-files", "-z"], root).split("\0"))
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts or (str(relative) not in tracked and any(part in IGNORED for part in relative.parts)):
            continue
        if path.is_symlink() and not path.resolve().is_relative_to(root.resolve()):
            raise PortfolioError(f"checkout contains an external symlink: {relative}")
        if path.is_symlink() or path.is_file():
            value = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
            digest.update(str(relative).encode() + b"\0")
            digest.update(str(path.lstat().st_mode & 0o170111).encode() + b"\0")
            digest.update(str(len(value)).encode() + b"\0" + value)
    return digest.hexdigest()


def default_branch(repo: Repository) -> str:
    source = Path(repo.source)
    if source.is_dir():
        try:
            return git(source, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").removeprefix("origin/")
        except PortfolioError:
            branches = git(source, "for-each-ref", "--format=%(refname:short)", "refs/heads/").splitlines()
            try:
                current = git(source, "symbolic-ref", "--short", "HEAD")
            except PortfolioError:
                current = ""
            if current in {"main", "master"} and current in branches:
                return current
            for branch in ("main", "master"):
                if branch in branches:
                    return branch
            branch = git(source, "symbolic-ref", "--short", "HEAD")
            if branch in branches:
                return branch
            raise PortfolioError(f"cannot determine local default branch: {repo.key}")
    lines = command(["git", "ls-remote", "--symref", repo.source, "HEAD"], Path.cwd()).splitlines()
    branch = next((line.split()[1].removeprefix("refs/heads/") for line in lines if line.startswith("ref:")), "")
    if not branch:
        raise PortfolioError(f"cannot determine default branch for {repo.key}")
    return branch


@contextlib.contextmanager
def source_auth(url: str, workflow: dict[str, Any]):
    host = urllib.parse.urlparse(url).hostname
    name = workflow.get("source_credentials", {}).get(host)
    if not name:
        name = {"github.com": "GH_TOKEN", "gitlab.com": "GITLAB_TOKEN"}.get(host)
    with git_credentials(os.environ.get(name, "") if name else "", url):
        yield


def ref_manifest(repo: Repository, scope: str, branch: str) -> dict[str, str]:
    source = Path(repo.source)
    if source.is_dir():
        text = git(source, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads/", "refs/tags/")
    else:
        text = command(["git", "ls-remote", "--heads", "--tags", repo.source], Path.cwd())
    values = {ref: sha for sha, ref in (line.split() for line in text.splitlines()) if not ref.endswith("^{}")}
    if scope == "default":
        values = {ref: sha for ref, sha in values.items() if ref == "refs/heads/" + branch}
    return values


def source_commit(repo: Repository) -> str:
    source = Path(repo.source)
    revision = repo.ref or default_branch(repo)
    if source.is_dir() and ((source / ".git").exists() or (source / "HEAD").is_file()):
        return git(source, "rev-parse", "--verify", f"{revision}^{{commit}}")
    if len(revision) == 40 and all(char in "0123456789abcdefABCDEF" for char in revision):
        return revision.lower()
    lines = command(["git", "ls-remote", repo.source, revision, f"refs/heads/{revision}",
                     f"refs/tags/{revision}", f"refs/tags/{revision}^{{}}"], Path.cwd()).splitlines()
    matches = [line.split()[0] for line in lines if line.endswith("^{}")]
    matches = matches or [line.split()[0] for line in lines]
    if not matches or len(set(matches)) != 1:
        raise PortfolioError(f"source ref cannot be resolved unambiguously: {repo.key}")
    return matches[0]


def canonical(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def run_directory(state: Path, run_id: str | None = None) -> Path:
    if run_id is None or run_id == "latest":
        run_id = read_json(state / "latest.json")["run_id"]
    if not run_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for char in run_id):
        raise PortfolioError("invalid run id")
    root = state / "runs" / run_id
    if not root.is_dir() or root.resolve().parent != (state / "runs").resolve():
        raise PortfolioError(f"run does not exist: {run_id}")
    return root


def create_run(state: Path, selected: Sequence[Repository], config: dict[str, Any], workflow: dict[str, Any]) -> Path:
    with lock(state / ".locks", "run-catalog:" + str(state.resolve()), "create run"):
        return _create_run(state, selected, config, workflow)


def _create_run(state: Path, selected: Sequence[Repository], config: dict[str, Any], workflow: dict[str, Any]) -> Path:
    run_id = now().replace(":", "-").replace("+", "-") + "-" + uuid.uuid4().hex[:8]
    root = state / "runs" / run_id
    root.mkdir(parents=True)
    write_json(root / "run.json", {
        "schema_version": 1, "run_id": run_id, "generated_at": now(), "status": "running",
        "selected": [repo.key for repo in selected], "repositories": [dataclasses.asdict(repo) for repo in selected],
        "config_hash": canonical(config), "config": config, "workflow": workflow,
        "stages": {}, "approvals": {}, "sources": {}, "dependency_override": False,
    })
    write_json(state / "latest.json", {"run_id": run_id})
    return root


def snapshot(repo: Repository, root: Path) -> tuple[Repository, dict[str, Any]]:
    workflow = read_json(root / "run.json")["workflow"]
    target = root / "sources" / repo.key
    source = Path(repo.source)
    remote = not source.is_dir()
    local_git = (source / ".git").exists() or ((source / "HEAD").is_file() and (source / "objects").is_dir())
    identity = {"source": repo.source, "ref": repo.ref, "selection": workflow["source_selection"]}
    if target.exists():
        metadata = target / ".git/java-update-snapshot.json"
        info = read_json(metadata)
        if info.get("identity") != identity or files_digest(target) != info["tree_hash"] or git(target, "rev-parse", "HEAD") != info["commit"]:
            raise PortfolioError(f"incomplete or changed source snapshot: {repo.key}; start a new run")
        return dataclasses.replace(repo, source=str(target), ref=None), info
    temporary = target.parent / ("." + repo.key + ".incomplete")
    target.parent.mkdir(parents=True, exist_ok=True)
    if temporary.is_symlink():
        raise PortfolioError("unsafe snapshot staging path")
    if temporary.exists():
        shutil.rmtree(temporary)
    with source_auth(repo.source, workflow):
        if remote or local_git:
            if local_git and git(source, "rev-parse", "--is-bare-repository") != "true" and git(source, "status", "--porcelain"):
                raise PortfolioError(f"local source must be clean before snapshotting: {repo.key}")
            branch = default_branch(repo)
            selection = repo.ref or ("HEAD" if not remote and workflow["source_selection"] == "current" else branch)
            selected_commit = source_commit(dataclasses.replace(repo, ref=selection))
            manifest = ref_manifest(repo, workflow["publishing"]["history"], branch)
            base = {**workflow["publishing"]["request"], **workflow["publishing"]["repositories"].get(repo.key, {}).get("request", {})}.get("base") or branch
            base_commit = source_commit(dataclasses.replace(repo, ref=base))
            args = ["git", "clone", "--no-tags", "--branch", branch]
            args += ["--depth", "1"] if remote else ["--no-hardlinks"]
            command([*args, "--", repo.source, str(temporary)], root)
            if remote and selection != branch:
                git(temporary, "fetch", "--depth", "1", "origin", selection)
            if not remote and selection == "HEAD":
                git(temporary, "fetch", "--no-tags", repo.source, selected_commit)
            try:
                git(temporary, "checkout", "--detach", selected_commit)
            except PortfolioError:
                git(temporary, "fetch", "--depth", "1", "origin", selected_commit)
                git(temporary, "checkout", "--detach", selected_commit)
            default_commit = manifest.get("refs/heads/" + branch)
            if not default_commit:
                raise PortfolioError("source default branch missing from captured manifest")
        else:
            if repo.ref or workflow["source_selection"] == "current":
                raise PortfolioError(f"cannot select a Git ref on a non-Git source: {repo.key}")
            branch, base = "main", "main"
            shutil.copytree(source, temporary, symlinks=True, ignore=shutil.ignore_patterns(*IGNORED))
            files_digest(temporary)
            git(temporary, "init", "-b", branch)
            git(temporary, "add", "--all")
            git(temporary, "commit", "--allow-empty", "-m", "Snapshot local migration input")
            selected_commit = default_commit = base_commit = git(temporary, "rev-parse", "HEAD")
            manifest = {"refs/heads/main": selected_commit}
    info = {"identity": identity, "source": repo.source, "ref": repo.ref, "commit": selected_commit,
            "default_branch": branch, "snapshot": str(target), "tree_hash": files_digest(temporary),
            "has_remote": remote or local_git, "default_commit": default_commit,
            "ref_manifest": manifest, "request_base": base, "request_base_commit": base_commit,
            "migration_branch": workflow["publishing"]["repositories"].get(repo.key, {}).get("source_branch", workflow["publishing"]["source_branch"]).format(
                java=read_json(root / "run.json")["config"]["targets"]["java"]["desired"], run_id=root.name)}
    # Older configured templates still get a unique per-run suffix.
    if root.name not in info["migration_branch"]:
        info["migration_branch"] += "-" + root.name
    git(temporary, "check-ref-format", "--branch", info["migration_branch"])
    write_json(temporary / ".git/java-update-snapshot.json", info)
    temporary.rename(target)
    event("snapshot-completed", repository=repo.key, commit=selected_commit, manifest=manifest)
    return dataclasses.replace(repo, source=str(target), ref=None), info


def snapshots(root: Path, portfolio: Portfolio, selected: Sequence[Repository]) -> tuple[Portfolio, list[Repository]]:
    receipt = read_json(root / "run.json")
    sources = receipt["sources"]
    replaced = {}
    for repo in selected:
        if repo.key not in sources:
            migrated, sources[repo.key] = snapshot(repo, root)
            replaced[repo.key] = migrated
            receipt["sources"] = sources
            write_json(root / "run.json", receipt)
        else:
            info = sources[repo.key]
            path = Path(info["snapshot"])
            if not info.get("ref_manifest"):
                raise PortfolioError("saved source lacks immutable refs; start a new run")
            if files_digest(path) != info["tree_hash"] or git(path, "rev-parse", "HEAD") != info["commit"]:
                raise PortfolioError(f"source snapshot changed: {repo.key}; start a new run")
            replaced[repo.key] = dataclasses.replace(repo, source=info["snapshot"], ref=None)
    return dataclasses.replace(portfolio, repositories=tuple(replaced.get(repo.key, repo) for repo in portfolio.repositories)), [replaced[repo.key] for repo in selected]


def checkpoint_hash(root: Path, stage: str) -> str:
    receipt = read_json(root / "run.json")
    values: dict[str, Any] = {"config_hash": receipt["config_hash"], "sources": receipt["sources"]}
    if STAGES.index(stage) >= 2 and (root / "planned-policy.json").exists():
        values["planned-policy"] = read_json(root / "planned-policy.json")
    for name in STAGES[:STAGES.index(stage) + 1]:
        for path in sorted((root / name).rglob("*.json")):
            # Nested worktrees/engine reports are separately bound by the output digest.
            parts = path.relative_to(root / name).parts
            if "attempts" not in parts and "history" not in parts:
                values[str(path.relative_to(root))] = read_json(path)
        if name == STAGES[4]:
            for path in sorted((root / name).rglob("*")):
                if path.is_file() and path.suffix in {".xml", ".tgf"} and "history" not in path.relative_to(root / name).parts:
                    values[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        if name == STAGES[3]:
            for path in sorted((root / name).glob("repositories/*/attempts/*/migration-policy.json")):
                values[str(path.relative_to(root))] = read_json(path)
    if STAGES.index(stage) >= 3:
        for key in receipt["selected"]:
            migration = root / STAGES[3] / "repositories" / key / "result.json"
            if migration.exists():
                data = read_json(migration)
                if data.get("output") and Path(data["output"]).is_dir():
                    values[f"output:{key}"] = files_digest(Path(data["output"]))
                    values[f"output-commit:{key}"] = git(Path(data["output"]), "rev-parse", "HEAD")
    return canonical(values)


def approve(root: Path, stage: str) -> None:
    receipt = read_json(root / "run.json")
    if receipt["stages"].get(stage, {}).get("status") not in {"complete", "prepared", "partial"}:
        raise PortfolioError(f"stage is not ready for approval: {stage}")
    receipt["approvals"][stage] = {"approved_at": now(), "fingerprint": checkpoint_hash(root, stage)}
    receipt["status"] = "approved"
    write_json(root / "run.json", receipt)
    event("approval", root, stage=stage, fingerprint=receipt["approvals"][stage]["fingerprint"])


def check_approvals(root: Path) -> None:
    receipt = read_json(root / "run.json")
    for stage, value in list(receipt["approvals"].items()):
        if value["fingerprint"] != checkpoint_hash(root, stage):
            index = STAGES.index(stage)
            for later in STAGES[index:]:
                receipt["approvals"].pop(later, None)
                if later != stage:
                    receipt["stages"].pop(later, None)
            receipt["status"] = "invalidated"
            write_json(root / "run.json", receipt)
            event("approval-invalidated", root, stage=stage)
            raise PortfolioError(f"{stage} artifacts changed; approval invalidated, review and revalidate before proceeding")


def compare_runs(root: Path, other: Path) -> Path:
    lines = [f"# Run comparison: {other.name} → {root.name}", "", "## Policy", "", "```diff"]
    previous = read_json(other / "run.json")
    current = read_json(root / "run.json")
    lines.extend(difflib.unified_diff(json.dumps(previous["config"], indent=2, sort_keys=True).splitlines(),
                                     json.dumps(current["config"], indent=2, sort_keys=True).splitlines(),
                                     fromfile=other.name, tofile=root.name, lineterm=""))
    lines.append("```")
    for key in sorted(set(current["selected"]) | set(previous["selected"])):
        lines.extend(["", f"## Repository {key}", ""])
        for kind in ("sources", "output"):
            def location(run: Path, receipt: dict[str, Any]) -> Path | None:
                if kind == "sources":
                    info = receipt["sources"].get(key)
                    return Path(info["snapshot"]) if info else None
                result = run / STAGES[3] / "repositories" / key / "result.json"
                return Path(read_json(result)["output"]) if result.exists() and read_json(result).get("output") else None
            left, right = location(other, previous), location(root, current)
            if left and right:
                process = subprocess.run(["git", "diff", "--no-index", "--", str(left), str(right)],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
                # Git metadata and generated build output are not useful source diffs.
                chunks = process.stdout.split("diff --git ")
                visible = [chunk for chunk in chunks if chunk and not any(f"/{name}/" in chunk.splitlines()[0] for name in IGNORED)]
                lines.extend([f"### {kind}", "", "```diff", *["diff --git " + chunk for chunk in visible], "```"])
    destination = root / "reports" / f"diff-{other.name}.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n")
    return destination
