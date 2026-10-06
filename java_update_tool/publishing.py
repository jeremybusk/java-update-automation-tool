"""History-preserving publishing with independent, retryable target receipts."""
from __future__ import annotations

import json
import contextlib
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Sequence

from .core import PortfolioError, Repository, STAGES, artifact_path, now, read_json, write_json
from .runs import command, files_digest, git, git_credentials, source_auth, ref_manifest, canonical
from .operations import lock, event, redact, destination_identity
from .policy import validate_location


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PortfolioError("provider redirect refused; configure the intended API host")


def remote_auth(url: str, options: dict[str, Any], workflow: dict[str, Any], target: str):
    if target == "src_repo":
        return source_auth(url, workflow)
    api = urllib.parse.urlparse(options.get("api_url") or ("https://api.github.com" if options["provider"] == "github" else "https://gitlab.com/api/v4"))
    expected = "github.com" if api.hostname == "api.github.com" else api.hostname
    token = ""
    if urllib.parse.urlparse(url).scheme == "https" and urllib.parse.urlparse(url).hostname == expected:
        try:
            token = Provider(options).token
        except PortfolioError:
            pass
    return git_credentials(token, url)


class Provider:
    def __init__(self, options: dict[str, Any]):
        self.kind = options["provider"]
        self.base = options.get("api_url") or ("https://api.github.com" if self.kind == "github" else "https://gitlab.com/api/v4")
        parsed = urllib.parse.urlparse(self.base)
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise PortfolioError("provider API URL must use HTTPS")
        env_name = options.get("token_env") or ("GH_TOKEN" if self.kind == "github" else "GITLAB_TOKEN")
        self.token = os.environ.get(env_name, "")
        if not self.token and self.kind == "github" and not options.get("token_env"):
            try:
                result = subprocess.run(["gh", "auth", "token"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, timeout=10, check=False)
                self.token = result.stdout.strip() if result.returncode == 0 else ""
            except (OSError, subprocess.TimeoutExpired):
                pass
        if not self.token:
            raise PortfolioError(f"provider authentication is missing: configure {env_name}")
        self.options = options

    def request(self, method: str, path: str, data: dict[str, Any] | None = None, missing: bool = False) -> Any:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers["Authorization" if self.kind == "github" else "PRIVATE-TOKEN"] = ("Bearer " if self.kind == "github" else "") + self.token
        request = urllib.request.Request(self.base.rstrip("/") + path, data=json.dumps(data).encode() if data is not None else None,
                                         headers=headers, method=method)
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if missing and exc.code == 404:
                return None
            raise PortfolioError(f"{self.kind} API request failed with HTTP {exc.code}") from exc
        except (OSError, ValueError) as exc:
            raise PortfolioError(f"{self.kind} API request could not finish") from exc

    def path(self, owner: str, name: str) -> str:
        if self.kind == "github":
            return f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"
        return "/projects/" + urllib.parse.quote(f"{owner}/{name}", safe="")

    def destination(self, owner: str, name: str, mode: str) -> dict[str, Any]:
        existing = self.request("GET", self.path(owner, name), missing=True)
        if mode == "autocreate":
            if existing:
                raise PortfolioError(f"destination already exists: {owner}/{name}; use explicit precreated mode")
            if self.kind == "github":
                account = self.request("GET", "/users/" + urllib.parse.quote(owner, safe=""))
                path = f"/orgs/{urllib.parse.quote(owner, safe='')}/repos" if account["type"] == "Organization" else "/user/repos"
                if path == "/user/repos" and self.request("GET", "/user")["login"].lower() != owner.lower():
                    raise PortfolioError("configured destination owner does not match authenticated GitHub user")
                existing = self.request("POST", path, {"name": name, "private": self.options["private"], "auto_init": False})
            else:
                namespace = self.options.get("namespace_id")
                if namespace is None:
                    matches = self.request("GET", "/namespaces?search=" + urllib.parse.quote(owner, safe=""))
                    namespace = next((item["id"] for item in matches if item["full_path"] == owner), None)
                if namespace is None:
                    raise PortfolioError("GitLab destination namespace could not be resolved; set namespace_id")
                existing = self.request("POST", "/projects", {"name": name, "path": name, "namespace_id": namespace,
                    "visibility": "private" if self.options["private"] else "public", "initialize_with_readme": False})
        elif not existing:
            raise PortfolioError(f"precreated destination does not exist: {owner}/{name}")
        url = existing["clone_url" if self.kind == "github" else "http_url_to_repo"]
        validate_location(url)
        return {"url": url,
                "owner": owner, "name": name}

    def default_branch(self, owner: str, name: str, branch: str) -> None:
        self.request("PATCH" if self.kind == "github" else "PUT", self.path(owner, name), {"default_branch": branch})


def full_history(output: Path, source: dict[str, Any], scope: str, workflow: dict[str, Any] | None = None) -> None:
    if not source["has_remote"]:
        return
    if not source.get("ref_manifest"):
        raise PortfolioError("saved source lacks immutable refs; start a new run")
    options = workflow or {}
    repo = Repository("source", source["source"], "source", "source")
    with source_auth(source["source"], options):
        current = ref_manifest(repo, scope, source["default_branch"])
        # The explicitly planned source-publication refs can have been created by
        # an earlier successful push before another operation failed.
        generated = {"refs/heads/" + source["migration_branch"]: git(output, "rev-parse", "HEAD")}
        previous_heads = set()
        publishing_receipt = artifact_path(Path(source["snapshot"]).parent.parent, STAGES[5], "repositories", Path(source["snapshot"]).name)
        if publishing_receipt.exists():
            saved = read_json(publishing_receipt)
            records = [saved.get("targets", {}).get("src_repo", {})]
            records += [item["receipt"] for item in saved.get("history", []) if item["target"] == "src_repo"]
            for record in records:
                head = record.get("git_receipt", {}).get("refs", {}).get(source["migration_branch"])
                if head:
                    git(output, "merge-base", "--is-ancestor", head, "HEAD")
                    previous_heads.add(head)
        rename = options.get("publishing", {}).get("source_default_branch")
        if rename:
            generated["refs/heads/" + rename] = source["default_commit"]
        for ref, sha in generated.items():
            owned_heads = previous_heads if ref == "refs/heads/" + source["migration_branch"] else set()
            if ref not in source["ref_manifest"] and current.get(ref) in {sha, *owned_heads}:
                current.pop(ref)
        if current != source["ref_manifest"]:
            raise PortfolioError("captured source refs changed; start a new reviewed run")
        if git(output, "rev-parse", "--is-shallow-repository") == "true":
            git(output, "fetch", "--unshallow", "--no-tags", source["source"], source["commit"])
        for ref, sha in source["ref_manifest"].items():
            destination = ref if ref.startswith("refs/tags/") else "refs/java-update/source/" + ref.removeprefix("refs/heads/")
            git(output, "fetch", "--no-tags", source["source"], f"{sha}:{destination}")
    if git(output, "rev-parse", "--is-shallow-repository") == "true":
        raise PortfolioError("source cannot provide full history; publishing remains blocked")


def provider_location(url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"https", "http", "ssh"} and not ("@" in url and ":" in url):
        raise PortfolioError("source default-branch changes require a GitHub/GitLab repository URL")
    path = parsed.path if parsed.scheme else url.split(":", 1)[-1]
    parts = path.strip("/").removesuffix(".git").split("/")
    if len(parts) < 2:
        raise PortfolioError("source default-branch changes require a GitHub/GitLab repository URL")
    return "/".join(parts[:-1]), parts[-1]


def publish_stage(selected: Sequence[Repository], config: dict[str, Any], workflow: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    receipt = read_json(root / "run.json")
    if receipt["stages"].get(STAGES[4], {}).get("status") not in {"complete", "partial"}:
        raise PortfolioError("publishing requires current completed validation; rerun stage 05-validation")
    publishing = workflow["publishing"]
    validation = {repo.key: read_json(artifact_path(root, STAGES[4], "repositories", repo.key)) for repo in selected}
    aliases = {alias: repo.key for repo in selected for alias in (repo.key, repo.repo_name)}
    assessments = list((root / STAGES[4] / "assessment" / STAGES[1]).glob("*/*/result.json"))
    cohort_data = [read_json(path) for path in assessments if path.parts[-3] != "repositories"]
    ready = set()
    for repo in selected:
        applicable = [item for item in cohort_data if
            (item["artifact_type"] == "application-assessment" and item["id"] == repo.application_id) or
            (item["artifact_type"] == "application-group-assessment" and item["id"] == repo.application_group_id)]
        if validation[repo.key]["status"] == "validated" and len(applicable) == 2 and all(item["status"] == "ready" for item in applicable):
            ready.add(repo.key)
    while True:
        retained = {repo.key for repo in selected if repo.key in ready and
                    all(name not in aliases or aliases[name] in ready for name in repo.depends_on)}
        if retained == ready:
            break
        ready = retained
    failures = any(item["status"] != "validated" for item in validation.values()) or any(item["status"] != "ready" for item in cohort_data)
    if receipt["dependency_override"]:
        raise PortfolioError("manual dependency override lacks compatibility evidence; publishing is blocked")
    if failures and not publishing["independent_applications"]:
        raise PortfolioError("required validation/cohort checks failed; publishing is blocked for this run")
    if not publishing["enabled"] and any(target != "local_repo" for target in publishing["targets"]):
        raise PortfolioError("remote publishing requires workflow.publishing.enabled: true")
    results = []
    for repo in selected:
        path = artifact_path(root, STAGES[5], "repositories", repo.key)
        result = read_json(path) if path.exists() else {
            "schema_version": 1, "artifact_type": "repository-publishing-result", "stage": STAGES[5],
            "generated_at": now(), "repository": repo.key, "status": "failed", "targets": {},
        }
        value = validation[repo.key]
        if repo.key not in ready:
            result.update({"status": "blocked", "error": "application or related cohort is not independently validated"})
            write_json(path, result)
            results.append(result)
            continue
        output = Path(value["output"])
        if files_digest(output) != value["tree_hash"] or git(output, "rev-parse", "HEAD") != value["commit"]:
            raise PortfolioError(f"validated output changed: {repo.key}; rerun validation")
        options = {**publishing, **publishing["repositories"].get(repo.key, {})}
        options["request"] = {**publishing["request"], **options["request"]}
        branch = options["default_branch"]
        git(output, "check-ref-format", "--branch", branch)
        source_branch = receipt["sources"][repo.key]["migration_branch"]
        git(output, "check-ref-format", "--branch", source_branch)
        try:
            full_history(output, receipt["sources"][repo.key], publishing["history"], {**workflow, "publishing": options})
        except PortfolioError as exc:
            result.update({"status": "failed", "error": f"full history could not be obtained: {exc}"})
            write_json(path, result)
            results.append(result)
            continue
        for target in publishing["targets"]:
            target_result = result["targets"].get(target, {})
            plan_hash = canonical({"target": target, "options": options, "history": receipt["sources"][repo.key]["ref_manifest"]})
            unchanged = target_result.get("commit") == value["commit"] and target_result.get("tree_hash") == value["tree_hash"] and target_result.get("plan_hash") == plan_hash
            if target_result.get("status") == "published" and not unchanged:
                result.setdefault("history", []).append({"target": target, "receipt": dict(target_result)})
                target_result = {key: item for key, item in target_result.items() if key in {"destination", "url", "branch"}}
            guard = contextlib.ExitStack()
            try:
                from .validation import verify_validation_evidence
                verify_validation_evidence(value)
                api = urllib.parse.urlparse(options.get("api_url") or ("https://api.github.com" if options["provider"] == "github" else "https://gitlab.com/api/v4"))
                host = "github.com" if api.hostname == "api.github.com" else api.netloc
                location = str(root / "local-repositories" / repo.key) if target == "local_repo" else repo.source if target == "src_repo" else options.get("url") or f"https://{host}/{options['owner']}/{options.get('name') or options['prefix'] + repo.repo_name}"
                identity = destination_identity(location)
                guard.enter_context(lock(root.parent.parent / ".locks", "destination:" + identity, f"publish {root.name} {repo.key} {target}"))
                event("publishing-started", repository=repo.key, target=target, commit=value["commit"])
                if target == "local_repo":
                    local = root / "local-repositories" / repo.key
                    extra = {}
                    if publishing["history"] == "all":
                        refs = git(output, "for-each-ref", "--format=%(objectname) %(refname)", "refs/java-update/source/")
                        for line in refs.splitlines():
                            sha, ref = line.split()
                            name = ref.removeprefix("refs/java-update/source/")
                            if name == receipt["sources"][repo.key]["default_branch"]:
                                continue
                            if name == branch and sha != value["commit"]:
                                raise PortfolioError(f"branch mapping collision: {name}")
                            extra[name] = sha
                    if not local.exists():
                        local.parent.mkdir(exist_ok=True)
                        argv = ["git", "clone", "--no-hardlinks", "--single-branch"]
                        if publishing["history"] == "default":
                            argv.append("--no-tags")
                        command([*argv, "--", str(output), str(local)], root)
                        previous_branch = git(local, "branch", "--show-current")
                        git(local, "checkout", "-B", branch, value["commit"])
                        if previous_branch and previous_branch != branch:
                            git(local, "branch", "-d", previous_branch)
                    elif git(local, "rev-parse", "HEAD") != value["commit"]:
                        if git(local, "status", "--porcelain") or git(local, "branch", "--show-current") != branch:
                            raise PortfolioError("retained local repository has human changes or a different checkout")
                        git(local, "fetch", "--no-tags", str(output), value["commit"])
                        git(local, "merge-base", "--is-ancestor", "HEAD", value["commit"])
                        git(local, "merge", "--ff-only", value["commit"])
                    if publishing["history"] == "all":
                        for ref, sha in receipt["sources"][repo.key]["ref_manifest"].items():
                            if ref.startswith("refs/tags/"):
                                existing_tag = git(local, "for-each-ref", "--format=%(objectname)", ref)
                                if existing_tag and existing_tag != sha:
                                    raise PortfolioError(f"local tag differs: {ref}")
                                git(local, "fetch", "--no-tags", str(output), f"{sha}:{ref}")
                    for name, sha in extra.items():
                        existing_local = git(local, "for-each-ref", "--format=%(objectname)", "refs/heads/" + name)
                        if existing_local and existing_local != sha:
                            raise PortfolioError(f"local branch differs: {name}")
                        if not existing_local:
                            git(local, "branch", name, sha)
                    if git(local, "rev-parse", "HEAD") != value["commit"] or files_digest(local) != value["tree_hash"]:
                        raise PortfolioError("retained local repository differs from validated output")
                    target_result.update({"url": str(local), "branch": branch})
                else:
                    if target == "src_repo":
                        if not receipt["sources"][repo.key]["has_remote"]:
                            raise PortfolioError("source is not a Git repository")
                        url, remote_branch = repo.source, source_branch
                    else:
                        remote_branch = branch
                        if options.get("url"):
                            if options["mode"] != "precreated":
                                raise PortfolioError("explicit destination URL requires precreated mode")
                            url = options["url"]
                        elif target_result.get("destination"):
                            # Reuse only a destination whose creation receipt belongs to this run.
                            url = target_result["destination"]["url"]
                        else:
                            name = options.get("name") or options["prefix"] + repo.repo_name
                            if not options["owner"] or (options["mode"] == "autocreate" and not options["prefix"] and not options.get("name")):
                                raise PortfolioError("configure destination owner and prefix or an explicit name")
                            destination = Provider(options).destination(options["owner"], name, options["mode"])
                            target_result["destination"] = destination
                            result["targets"][target] = target_result
                            write_json(path, result)  # Preserve creation before a potentially failed push.
                            url = destination["url"]
                        # Preflight every branch/tag before sending any of them.
                    actual_identity = destination_identity(url)
                    if actual_identity != identity:
                        guard.enter_context(lock(root.parent.parent / ".locks", "destination:" + actual_identity, f"publish {root.name} {repo.key} {target}"))
                    full_history(output, receipt["sources"][repo.key], publishing["history"], {**workflow, "publishing": options})
                    with remote_auth(url, options, workflow, target):
                        refs = {remote_branch: value["commit"]}
                        renamed_source = options.get("source_default_branch") if target == "src_repo" else None
                        if renamed_source:
                            source_owner, source_name = provider_location(url)
                            from .reviews import source_provider_options
                            source_provider = Provider(source_provider_options(url, options, workflow))
                            git(output, "check-ref-format", "--branch", renamed_source)
                            default = receipt["sources"][repo.key]["default_branch"]
                            tip = command(["git", "ls-remote", "--heads", url, "refs/heads/" + default], output)
                            current = tip.split()[0] if tip else None
                            expected = receipt["sources"][repo.key]["default_commit"]
                            if current != expected:
                                raise PortfolioError("source default branch changed; start a new reviewed run before renaming")
                            if renamed_source in refs and refs[renamed_source] != current:
                                raise PortfolioError("source branch mapping collision")
                            refs[renamed_source] = current
                        if target == "dst_repo" and publishing["history"] == "all":
                            for line in git(output, "for-each-ref", "--format=%(objectname) %(refname)", "refs/java-update/source/").splitlines():
                                sha, ref = line.split()
                                name = ref.removeprefix("refs/java-update/source/")
                                if name == receipt["sources"][repo.key]["default_branch"]:
                                    continue
                                if name in refs and refs[name] != sha:
                                    raise PortfolioError(f"branch mapping collision: {name}")
                                refs[name] = sha
                        remote_refs = command(["git", "ls-remote", "--heads", "--tags", url], output)
                        existing = {ref: sha for sha, ref in (line.split() for line in remote_refs.splitlines())}
                        refspecs = []
                        for name, sha in refs.items():
                            ref = "refs/heads/" + name
                            if ref in existing:
                                git(output, "fetch", "--no-tags", url, ref)
                                git(output, "merge-base", "--is-ancestor", existing[ref], sha)
                            refspecs.append(f"{sha}:{ref}")
                        if target == "dst_repo" and publishing["history"] == "all":
                            for line in git(output, "for-each-ref", "--format=%(objectname) %(refname)", "refs/tags/").splitlines():
                                sha, ref = line.split()
                                if ref in existing and existing[ref] != sha:
                                    raise PortfolioError(f"destination tag differs: {ref}")
                                refspecs.append(f"{sha}:{ref}")
                        matches = all(existing.get(spec.split(":", 1)[1]) == spec.split(":", 1)[0] for spec in refspecs)
                        if not matches:
                            command(["git", "push", "--atomic", "--", url, *refspecs], output)
                        target_result["git_receipt"] = {"commit": value["commit"], "refs": dict(refs), "url": url}
                        result["targets"][target] = target_result
                        write_json(path, result)
                        if target == "dst_repo" and target_result.get("destination"):
                            destination = target_result["destination"]
                            Provider(options).default_branch(destination["owner"], destination["name"], remote_branch)
                        if renamed_source:
                            source_provider.default_branch(source_owner, source_name, renamed_source)
                    target_result.update({"url": url, "branch": remote_branch})
                target_result.update({"status": "published", "commit": value["commit"], "tree_hash": value["tree_hash"], "plan_hash": plan_hash,
                                      "published_at": target_result.get("published_at", now())})
                target_result.pop("error", None)
            except (PortfolioError, OSError, ValueError) as exc:
                target_result.update({"status": "failed", "error": str(exc)})
            finally:
                guard.close()
            result["targets"][target] = target_result
            write_json(path, result)
            event("publishing-finished", repository=repo.key, target=target, status=target_result["status"])
        request_options = {**publishing["request"], **options.get("request", {})}
        if request_options["enabled"] and result["targets"].get("src_repo", {}).get("status") == "published":
            from .reviews import ensure_request
            result["request"] = ensure_request(repo, options, workflow, receipt["sources"][repo.key], value, root, result.get("request", {}))
        result["status"] = "published" if all(result["targets"].get(target, {}).get("status") == "published" for target in publishing["targets"]) else "failed"
        if result.get("request", {}).get("status") == "failed":
            result["status"] = "failed"
        write_json(path, result)
        results.append(result)
    return results
