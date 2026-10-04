"""Idempotent hosted draft-review requests; human state remains authoritative."""
from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

from .core import PortfolioError, Repository, artifact_path, read_json, write_json
from .maintenance import export_evidence
from .operations import event, lock, redact, destination_identity
from .runs import command


def source_provider_options(url: str, options: dict, workflow: dict) -> dict:
    from .publishing import provider_location
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or (url.split("@", 1)[-1].split(":", 1)[0] if "@" in url else None)
    api = urllib.parse.urlparse(options.get("api_url") or "")
    result = dict(options)
    if host == "github.com":
        result.update(provider="github", api_url="https://api.github.com")
    elif host == "gitlab.com":
        result.update(provider="gitlab", api_url="https://gitlab.com/api/v4")
    elif not host or host != api.hostname:
        raise PortfolioError("source review/default-branch API needs a matching configured provider host")
    result["token_env"] = workflow.get("source_credentials", {}).get(host)
    if host == api.hostname or (host == "github.com" and api.hostname == "api.github.com"):
        result["token_env"] = result["token_env"] or options.get("token_env")
    provider_location(url)
    return result


def ensure_request(repo: Repository, options: dict, workflow: dict, source: dict, validation: dict,
                   root: Path, previous: dict) -> dict:
    from .publishing import Provider, provider_location
    from .runs import source_auth
    result = dict(previous)
    resource = artifact_path(root, "06-publishing", "requests", repo.key)
    branch = source["migration_branch"]
    base = source["request_base"]
    if options.get("source_default_branch") and base == source["default_branch"]:
        base = options["source_default_branch"]
    marker = f"<!-- java-update run={root.name} repository={repo.key} -->"
    identity = {"run": root.name, "repository": repo.key, "head": validation["commit"], "head_branch": branch,
                "base": base, "base_commit": source["request_base_commit"]}
    try:
        with lock(root.parent.parent / ".locks", "destination:" + destination_identity(repo.source), f"review {root.name} {repo.key}"):
            provider = Provider(source_provider_options(repo.source, options, workflow))
            owner, name = provider_location(repo.source)
            endpoint = provider.path(owner, name)
            suffix = "/pulls" if provider.kind == "github" else "/merge_requests"
            query = urllib.parse.urlencode({"state": "all", "head": owner + ":" + branch, "base": base, "per_page": 100}) if provider.kind == "github" else urllib.parse.urlencode({"state": "all", "source_branch": branch, "target_branch": base, "per_page": 100})
            candidates = []
            for page in range(1, 101):
                batch = provider.request("GET", endpoint + suffix + "?" + query + f"&page={page}")
                candidates.extend(batch)
                if len(batch) < 100:
                    break
            else:
                raise PortfolioError("too many matching requests to reconcile safely")
            matching = []
            for candidate in candidates:
                detail = provider.request("GET", endpoint + suffix + "/" + str(candidate["number" if provider.kind == "github" else "iid"]))
                body = detail.get("body", "") if provider.kind == "github" else detail.get("description", "")
                head = detail.get("head", {}).get("sha") if provider.kind == "github" else detail.get("sha")
                head_branch = detail.get("head", {}).get("ref") if provider.kind == "github" else detail.get("source_branch")
                request_base = detail.get("base", {}).get("ref") if provider.kind == "github" else detail.get("target_branch")
                number = detail["number" if provider.kind == "github" else "iid"]
                same_receipt = result.get("id") == number and result.get("identity") == identity
                if (marker in (body or "") or same_receipt) and head == identity["head"] and head_branch == branch and request_base == base:
                    matching.append(detail)
            if len(matching) > 1:
                raise PortfolioError("multiple requests match this run; operator resolution required")
            if matching:
                detail = matching[0]
                state = detail["state"]
                if state not in {"open", "opened"} or detail.get("merged"):
                    raise PortfolioError("request is closed or merged; resolve explicitly before continuing")
                # Reconcile without PATCH/PUT: preserve readiness, title, body, and reviewer edits.
                result.update(status="created", id=detail["number" if provider.kind == "github" else "iid"],
                              url=detail["html_url" if provider.kind == "github" else "web_url"], identity=identity)
                result.pop("error", None)
                event("request-reconciled", repository=repo.key, request=result["id"])
            else:
                if result.get("id"):
                    raise PortfolioError("recorded request no longer matches; resolve explicitly")
                with source_auth(repo.source, workflow):
                    tip = command(["git", "ls-remote", "--heads", repo.source, "refs/heads/" + base], root)
                if not tip or tip.split()[0] != identity["base_commit"]:
                    raise PortfolioError("request base commit changed; start a new reviewed run")
                bundle = root / "evidence" / (repo.key + "-" + validation["commit"] + ".tar.gz")
                if not bundle.exists():
                    export_evidence(root, bundle)
                targets = read_json(root / "run.json")["config"]["targets"]
                lines = [marker, f"Validated commit: `{validation['commit']}`", f"Run: `{root.name}`",
                         "Validation outcome: " + validation["status"],
                         f"Policy identity: `{validation['compatibility_hash']}`",
                         f"Java target: `{targets['java']['desired']}`; acceptable: {targets['java']['acceptable']}",
                         f"Spring Boot target: `{targets['spring_boot']['desired']}`; acceptable: {targets['spring_boot']['acceptable']}",
                         "", "Validation scope: " + validation["scope"]["coverage"],
                         "Included builds: " + ", ".join(validation["scope"]["included"])]
                for excluded, reason in validation["scope"]["excluded"].items():
                    lines.append(f"Excluded build: `{excluded}` — {reason}")
                for membership in validation["scope"].get("memberships", []):
                    lines.append(f"Build relationship: `{membership['parent']}` → `{membership['member']}` ({membership['relationship']})")
                for proof in validation["test_evidence"]:
                    lines.append(f"Suite `{proof['suite']}` ({proof['build_root']}): {proof['status']}; {proof['counts']}")
                    if proof.get("reason"):
                        lines.append("Exemption: " + proof["reason"])
                for check in validation["checks"]:
                    lines.append(f"Check `{check['name']}`: {check['status']}")
                lines.append("Local evidence bundle: " + bundle.name)
                lines.extend(options["request"]["links"])
                body = redact("\n".join(lines))
                data = {"title": f"Update Java: {repo.repo_name} ({root.name})", "head": branch, "base": base, "body": body, "draft": True} if provider.kind == "github" else {
                    "title": f"Draft: Update Java: {repo.repo_name} ({root.name})", "source_branch": branch,
                    "target_branch": base, "description": body}
                result.update(status="creating", identity=identity, evidence=str(bundle))
                write_json(resource, result)  # Ambiguous responses can be reconciled on the next invocation.
                detail = provider.request("POST", endpoint + suffix, data)
                # The provider response must identify the requested validated head/base.
                head = detail.get("head", {}).get("sha") if provider.kind == "github" else detail.get("sha")
                returned_base = detail.get("base", {}).get("ref") if provider.kind == "github" else detail.get("target_branch")
                if head != identity["head"] or returned_base != base:
                    raise PortfolioError("created request response does not match validated head/base; reconcile before continuing")
                result.update(status="created", id=detail["number" if provider.kind == "github" else "iid"],
                              url=detail["html_url" if provider.kind == "github" else "web_url"])
                result.pop("error", None)
                event("request-created", repository=repo.key, request=result["id"])
    except (PortfolioError, OSError, ValueError, KeyError, TypeError) as exc:
        result.update(status="failed", error=redact(str(exc)), identity=identity)
        event("request-failed", repository=repo.key, error=str(exc))
    write_json(resource, result)
    return result
