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
from pathlib import Path
from typing import Any, Sequence

from .core import Portfolio, PortfolioError, Repository, STAGES, now, read_json, write_json

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
            result = subprocess.run(list(argv), cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PortfolioError(f"command could not finish: {argv[0]}") from exc
    if result.returncode:
        # Git errors can contain credential URLs; do not persist raw stderr.
        raise PortfolioError(f"{argv[0]} command failed with exit code {result.returncode}")
    return result.stdout.strip()


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


def source_commit(repo: Repository) -> str:
    source = Path(repo.source)
    if source.is_dir() and ((source / ".git").exists() or (source / "HEAD").is_file()):
        return git(source, "rev-parse", "--verify", f"{repo.ref or 'HEAD'}^{{commit}}")
    revision = repo.ref or "HEAD"
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
    target = root / "sources" / repo.key
    source = Path(repo.source)
    remote = not source.is_dir()
    local_git = (source / ".git").exists() or ((source / "HEAD").is_file() and (source / "objects").is_dir())
    if remote:
        refs = command(["git", "ls-remote", "--symref", repo.source, "HEAD"], root)
        default_branch = next((line.split()[1].removeprefix("refs/heads/") for line in refs.splitlines()
                               if line.startswith("ref:")), "")
        if not default_branch:
            raise PortfolioError(f"cannot determine default branch for {repo.key}")
        args = ["git", "clone", "--depth", "1", "--no-tags", "--branch", default_branch,
                "--", repo.source, str(target)]
        command(args, root)
        if repo.ref:
            git(target, "fetch", "--depth", "1", "origin", repo.ref)
            git(target, "checkout", "--detach", "FETCH_HEAD")
    elif local_git:
        bare = git(source, "rev-parse", "--is-bare-repository") == "true"
        if not bare and git(source, "status", "--porcelain"):
            raise PortfolioError(f"local source must be clean before snapshotting: {repo.key}")
        try:
            default_branch = git(source, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").removeprefix("origin/")
        except PortfolioError:
            try:
                default_branch = git(source, "symbolic-ref", "--short", "HEAD")
            except PortfolioError:
                branches = git(source, "for-each-ref", "--format=%(refname:short)", "refs/heads/").splitlines()
                default_branch = next((name for name in ("main", "master") if name in branches), "")
                if not default_branch:
                    raise PortfolioError(f"cannot determine default branch for detached local source: {repo.key}")
        command(["git", "clone", "--no-hardlinks", "--", repo.source, str(target)], root)
    else:
        if repo.ref:
            raise PortfolioError(f"cannot select a Git ref on a non-Git source: {repo.key}")
        default_branch = "main"
        shutil.copytree(source, target, symlinks=True, ignore=shutil.ignore_patterns(*IGNORED))
        files_digest(target)
        git(target, "init", "-b", default_branch)
        git(target, "add", "--all")
        git(target, "commit", "--allow-empty", "-m", "Snapshot local migration input")
    if repo.ref and not remote:
        # Local clone branches other than HEAD live under origin/.
        try:
            revision = git(target, "rev-parse", "--verify", f"{repo.ref}^{{commit}}")
        except PortfolioError:
            revision = git(target, "rev-parse", "--verify", f"origin/{repo.ref}^{{commit}}")
        git(target, "checkout", "--detach", revision)
    sha = git(target, "rev-parse", "HEAD")
    default_commit = git(target, "rev-parse", f"origin/{default_branch}") if remote or local_git else sha
    return dataclasses.replace(repo, source=str(target), ref=None), {
        "source": repo.source, "ref": repo.ref, "commit": sha, "default_branch": default_branch,
        "snapshot": str(target), "tree_hash": files_digest(target), "has_remote": remote or local_git,
        "default_commit": default_commit,
    }


def snapshots(root: Path, portfolio: Portfolio, selected: Sequence[Repository]) -> tuple[Portfolio, list[Repository]]:
    receipt = read_json(root / "run.json")
    sources = receipt["sources"]
    replaced = {}
    for repo in selected:
        if repo.key not in sources:
            migrated, sources[repo.key] = snapshot(repo, root)
            replaced[repo.key] = migrated
        else:
            info = sources[repo.key]
            path = Path(info["snapshot"])
            if files_digest(path) != info["tree_hash"] or git(path, "rev-parse", "HEAD") != info["commit"]:
                raise PortfolioError(f"source snapshot changed: {repo.key}; start a new run")
            replaced[repo.key] = dataclasses.replace(repo, source=info["snapshot"], ref=None)
    receipt["sources"] = sources
    write_json(root / "run.json", receipt)
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
