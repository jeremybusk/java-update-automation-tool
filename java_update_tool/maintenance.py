"""Portable evidence exports and conservative, explicit retained-run pruning."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import tarfile
import tempfile
from pathlib import Path

from .core import PortfolioError, read_json, write_json
from .operations import BusyError, event, lock, redact


def export_evidence(root: Path, destination: Path) -> Path:
    if root.resolve().is_relative_to(destination.resolve()):
        raise PortfolioError("evidence export cannot replace a run directory")
    if destination.exists():
        raise PortfolioError("evidence export destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="java-update-evidence-") as temporary:
        staging = Path(temporary)
        manifest = {}
        selected = [root / "run.json", root / "planned-policy.json", root / "tool-versions.json", root / "events.jsonl"]
        selected += list(root.glob("0[1-6]-*/**/*.json"))
        selected += list((root / "05-validation").rglob("*.xml"))
        selected += list((root / "05-validation").rglob("*.tgf"))
        selected += list((root / "reports").rglob("*.md"))
        selected += list((root / "diagnostics").glob("*"))
        for path in sorted(set(selected)):
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                continue
            relative = path.relative_to(root)
            if any(part in {".git", "worktrees", "output", "history"} for part in relative.parts) or (
                    "attempts" in relative.parts and path.name != "migration-policy.json"):
                continue
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".json":
                data = json.dumps(redact(read_json(path)), indent=2).encode()
            else:
                data = redact(path.read_text(errors="replace")).encode()
            target.write_bytes(data)
            manifest[str(relative)] = hashlib.sha256(data).hexdigest()
        write_json(staging / "manifest.json", {"schema_version": 1, "run": root.name, "files": manifest,
                   "purpose": "portable inspection; original host state is required for resume"})
        with tarfile.open(destination, "x:gz") as archive:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    archive.add(path, arcname=str(path.relative_to(staging)), recursive=False)
    event("evidence-exported", root, destination=str(destination), files=len(manifest))
    return destination


def prune(state: Path, options: dict, apply: bool = False) -> list[dict]:
    with lock(state / ".locks", "run-catalog:" + str(state.resolve()), "prune retained runs"):
        return _prune(state, options, apply)


def _prune(state: Path, options: dict, apply: bool) -> list[dict]:
    runs = state / "runs"
    receipts = {path.parent: read_json(path) for path in runs.glob("*/run.json") if not path.parent.is_symlink()}
    protected = {}
    def protect(path: Path, reason: str):
        resolved = path.resolve()
        for root in receipts:
            if resolved == root.resolve() or resolved.is_relative_to(root.resolve()):
                protected.setdefault(root, []).append(reason)
    for pin in options["retention"]["pins"]:
        protect(runs / pin if pin in {root.name for root in receipts} else Path(pin).expanduser(), "explicit retention pin")
    for reference in options["dependency_evidence"].values():
        protect(Path(reference).expanduser(), "configured dependency evidence")
    latest = state / "latest.json"
    if latest.exists():
        protect(runs / read_json(latest)["run_id"], "latest run")
    # Examine durable JSON receipts for cross-run dependencies, output paths, and pins.
    def references(value, owner):
        if isinstance(value, dict):
            for item in value.values():
                references(item, owner)
        elif isinstance(value, list):
            for item in value:
                references(item, owner)
        elif isinstance(value, str) and value.startswith("/"):
            path = Path(value)
            for other in receipts:
                if other != owner and path.resolve().is_relative_to(other.resolve()):
                    protect(path, f"referenced by {owner.name}")
    for root, receipt in receipts.items():
        if receipt["status"] not in {"complete", "published"}:
            protect(root, f"status {receipt['status']}: active, pending, or retryable")
        if (root / "local-repositories").exists():
            protect(root, "durable local publishing repositories")
        for path in root.rglob("*.json"):
            if any(part in {".git", "diagnostics", "worktrees", "output"} for part in path.relative_to(root).parts):
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                protect(root, "linked evidence; inspect before pruning")
                continue
            try:
                references(read_json(path), root)
            except PortfolioError:
                protect(root, "unreadable evidence; inspect before pruning")
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=options["retention"]["days"])
    results = []
    for root, receipt in receipts.items():
        reasons = protected.get(root, [])
        if dt.datetime.fromisoformat(receipt["generated_at"]) >= cutoff:
            reasons = [*reasons, "within retention period"]
        decision = {"run": root.name, "path": str(root), "action": "protected" if reasons else "would-delete", "reasons": reasons}
        try:
            with lock(state / ".locks", str(root.resolve()), "prune"):
                # A completed run may have resumed between the initial scan and
                # acquiring its lock. Reconcile before making deletion final.
                if read_json(root / "run.json") != receipt or (root / "local-repositories").exists():
                    decision.update(action="protected", reasons=[*reasons, "run changed during retention scan"])
                    results.append(decision)
                    continue
                if not reasons and apply:
                    event("cleanup", root, action="delete-run")
                    # Keep an operational deletion receipt outside the deleted run.
                    event("cleanup", state, run_id=root.name, action="delete-run")
                    shutil.rmtree(root)
                    decision["action"] = "deleted"
        except BusyError as exc:
            decision.update(action="protected", reasons=[*reasons, str(exc)])
        results.append(decision)
    event("prune", state, apply=apply, decisions=results)
    return results
