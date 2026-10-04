"""Local mutation locks, redacted diagnostics, and append-only operational events."""
from __future__ import annotations

import contextlib
import contextvars
import fcntl
import hashlib
import json
import os
import re
import socket
import signal
import subprocess
import threading
import time
import uuid
import urllib.parse
from pathlib import Path
from typing import Any

from .core import PortfolioError, now, write_json

CONTEXT = contextvars.ContextVar("operation_context", default=None)
DEFAULT_DIAGNOSTICS = {"check_bytes": 10 * 1024 * 1024, "run_bytes": 100 * 1024 * 1024, "retention_days": 30}


class BusyError(PortfolioError):
    """An existing local writer owns this resource."""


def destination_identity(location: str) -> str:
    """Share locks across local/file and HTTPS/SSH spellings of a Git target."""
    parsed = urllib.parse.urlparse(location)
    if parsed.scheme == "file":
        return str(Path(urllib.parse.unquote(parsed.path)).resolve())
    if not parsed.scheme and "@" in location and ":" in location:
        host, path = location.split("@", 1)[1].split(":", 1)
    elif parsed.hostname:
        host, path = parsed.hostname, parsed.path
        if parsed.port and parsed.port not in {22, 80, 443}:
            host += ":" + str(parsed.port)
    else:
        return str(Path(location).expanduser().resolve())
    path = path.strip("/").removesuffix(".git")
    if host.lower() == "github.com":
        path = path.lower()
    return host.lower() + "/" + path


def redact(value: Any, environment: dict[str, str] | None = None) -> Any:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if re.search(r"token|password|secret|authorization", str(key), re.I)
                else redact(item, environment) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, environment) for item in value]
    if not isinstance(value, str):
        return value
    for key, secret in (environment if environment is not None else os.environ).items():
        if len(secret) >= 4 and re.search(r"TOKEN|PASSWORD|SECRET|PRIVATE_KEY|API_KEY", key, re.I):
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", value)
    value = re.sub(r"(?i)(Bearer\s+|(?:token|password|secret|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]+", r"\1[REDACTED]", value)
    return value


@contextlib.contextmanager
def lock(directory: Path, identity: str, command: str):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(identity.encode()).hexdigest() + ".lock")
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read() or "owner details pending"
            raise BusyError(f"resource busy: {identity}; owner: {owner}") from exc
        handle.seek(0)
        handle.truncate()
        json.dump(redact({"pid": os.getpid(), "host": socket.gethostname(), "command": command, "started_at": now()}), handle)
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    # Keep the inode: unlinking a lock can admit writers holding its previous inode.


@contextlib.contextmanager
def session(root: Path, options: dict[str, Any]):
    marker = CONTEXT.set({"root": root, "options": options, "invocation": uuid.uuid4().hex})
    try:
        expire_logs(root, options.get("diagnostics", DEFAULT_DIAGNOSTICS))
        yield
    finally:
        CONTEXT.reset(marker)


def event(kind: str, root: Path | None = None, **values: Any) -> None:
    context = CONTEXT.get() or {}
    root = root or context.get("root")
    if root is None or not root.is_dir():
        return
    record = redact({"time": now(), "event": kind, "run": root.name,
                     "invocation": context.get("invocation"), **values})
    with (root / ".journal.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        with (root / "events.jsonl").open("a") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def expire_logs(root: Path, options: dict[str, Any]) -> None:
    cutoff = time.time() - options["retention_days"] * 86400
    directory = root / "diagnostics"
    if directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve()):
        raise PortfolioError("unsafe diagnostics directory outside retained run")
    for path in directory.glob("*.log"):
        if path.is_symlink():
            continue
        if path.stat().st_mtime < cutoff:
            path.unlink()
            event("diagnostic-expired", root, resource=path.name)


def execute(argv: list[str], cwd: Path, env: dict[str, str], timeout: int, require_complete: bool = False,
            include_stderr: bool = False) -> str:
    context = CONTEXT.get()
    options = context["options"].get("diagnostics", DEFAULT_DIAGNOSTICS) if context else DEFAULT_DIAGNOSTICS
    limit = options["check_bytes"]
    secrets = [secret for name, secret in env.items() if len(secret) >= 4 and
               re.search(r"TOKEN|PASSWORD|SECRET|PRIVATE_KEY|API_KEY", name, re.I)]
    capture_limit = limit + max((len(secret.encode()) for secret in secrets), default=0)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}
    started = time.monotonic()
    identifier = uuid.uuid4().hex
    code, error = None, None
    event("check-started", check=identifier, command=argv, cwd=str(cwd))
    def drain(stream, name):
        while chunk := stream.read(65536):
            available = max(0, capture_limit - len(buffers[name]))
            buffers[name].extend(chunk[:available])
            truncated[name] |= len(chunk) > available or len(buffers[name]) > limit
        stream.close()
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        readers = [threading.Thread(target=drain, args=(getattr(process, name), name), daemon=True) for name in buffers]
        for reader in readers:
            reader.start()
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            code = process.wait()
            error = "command timed out"
        finally:
            for reader in readers:
                reader.join(max(0, started + timeout - time.monotonic()))
            if any(reader.is_alive() for reader in readers):
                error = error or "command output timed out"
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                for reader in readers:
                    reader.join(1)
    except OSError as exc:
        error = str(exc)
    stdout = bytes(buffers["stdout"]).decode(errors="replace")
    stderr = bytes(buffers["stderr"]).decode(errors="replace")
    def safe_output(value, was_truncated):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
            if was_truncated:
                # A capture boundary can end within a credential. Never retain
                # that trailing prefix even when earlier redactions shorten text.
                for length in range(min(len(secret) - 1, len(value)), 0, -1):
                    if value.endswith(secret[:length]):
                        value = value[:-length] + "[REDACTED]"
                        break
        return redact(value)
    stdout = safe_output(stdout, truncated["stdout"])
    stderr = safe_output(stderr, truncated["stderr"])
    metadata = {"check": identifier, "command": argv, "cwd": str(cwd), "exit_code": code, "duration_seconds": round(time.monotonic() - started, 3),
                "truncated": any(truncated.values()), "error": error, "finished_at": now()}
    if context:
        directory = context["root"] / "diagnostics"
        directory.mkdir(exist_ok=True)
        used = sum(p.stat().st_size for p in directory.glob("*.log") if not p.is_symlink())
        allowed = max(0, min(limit, options["run_bytes"] - used))
        data = redact("STDOUT\n" + stdout + "\nSTDERR\n" + stderr).encode()
        metadata["truncated"] |= len(data) > allowed
        # Redact before imposing the persistent output bound.
        (directory / (identifier + ".log")).write_bytes(data[:allowed])
        metadata["log"] = str(directory / (identifier + ".log"))
        write_json(directory / (identifier + ".json"), redact(metadata, env))
        context["last_check"] = redact(metadata, env)
    event("check-finished", **metadata)
    if error or code != 0 or (require_complete and truncated["stdout"]):
        detail = error or ("command output exceeded capture limit" if truncated["stdout"] else f"exit code {code}")
        resource = f"; diagnostics: {metadata['log']}" if metadata.get("log") else ""
        raise PortfolioError(redact(f"{argv[0]} command failed: {detail}{resource}"))
    return (stdout + ("\n" + stderr if include_stderr else "")).strip()


def versions(root: Path) -> None:
    """Versions are durable metadata, separate from expiring diagnostic text."""
    values = {}
    for tool, args in (("git", ["--version"]), ("java", ["-version"]), ("mvn", ["-version"]), ("gradle", ["--version"])):
        import shutil
        if not shutil.which(tool):
            values[tool] = "unavailable"
            continue
        # Java writes its version to stderr; capture it through a bounded check.
        try:
            result = subprocess.run([tool, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=30, text=True, check=False)
            values[tool] = redact(result.stdout[:16384])
        except (OSError, subprocess.TimeoutExpired):
            values[tool] = "unavailable"
    write_json(root / "tool-versions.json", values)
