"""Resolve recipe binaries before source fallback, without executing recipes."""
import base64
import hashlib
import http.client
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .recipe_sources import DEFAULT_CACHE, DEFAULT_LOCK, cache_valid, digest, source_order

CENTRAL = "https://repo.maven.apache.org/maven2"


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        redirected = super().redirect_request(request, response, code, message, headers, new_url)
        if redirected is not None:
            original, target = urllib.parse.urlsplit(request.full_url), urllib.parse.urlsplit(new_url)
            if (original.scheme, original.netloc) != (target.scheme, target.netloc):
                redirected.remove_header("Authorization")
        return redirected


def try_repository(artifacts: list[str], *, cache: Path = DEFAULT_CACHE, lock_path: Path = DEFAULT_LOCK,
                   remote: str | None = None, username_env: str = "CODE_GENOME_USERNAME",
                   token_env: str = "CODE_GENOME_TOKEN", env: dict[str, str] | None = None) -> Path | None:
    env = os.environ if env is None else env
    lock = json.loads(lock_path.read_text())
    # Optional provided language APIs are needed to compile sources, not to run Java recipes.
    modules = [item for item in source_order(lock, artifacts) if not item.get("java_api_only")]
    remote = (remote or CENTRAL).rstrip("/")
    coordinates = sorted(item["artifact"] for item in modules)
    key = hashlib.sha256((digest(lock_path) + remote + json.dumps(coordinates)).encode()).hexdigest()[:24]
    directory = cache.expanduser().resolve() / "binaries" / key
    repository, receipt = directory / "maven", directory / "receipt.json"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    import fcntl
    with (directory / "download.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return _download(modules, remote, env, username_env, token_env, repository, receipt)


def _download(modules, remote, env, username_env, token_env, repository, receipt):
    if cache_valid(receipt, repository):
        print("Recipe binary cache: " + str(repository), flush=True)
        return repository
    opener = urllib.request.build_opener(SafeRedirect())
    headers = {}
    username, token = env.get(username_env), env.get(token_env)
    if username and token:
        headers["Authorization"] = "Basic " + base64.b64encode((username + ":" + token).encode()).decode()
    files = {}
    try:
        for module in modules:
            group, artifact, version = module["artifact"].split(":")
            base = group.replace(".", "/") + "/" + artifact + "/" + version + "/" + artifact + "-" + version
            for extension in ("pom", "jar"):
                name = base + "." + extension
                # Credentials belong only to an explicitly configured endpoint.
                request = urllib.request.Request(remote + "/" + name, headers=headers if remote != CENTRAL else {})
                with opener.open(request, timeout=60) as response:
                    data = response.read()
                if extension == "pom":
                    ET.fromstring(data)
                else:
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        if not archive.namelist():
                            raise ValueError("empty recipe JAR")
                destination = repository / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
                files[name] = hashlib.sha256(data).hexdigest()
    except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException,
            ET.ParseError, zipfile.BadZipFile, ValueError) as exc:
        status = "HTTP " + str(exc.code) if isinstance(exc, urllib.error.HTTPError) else "artifact resolution failed"
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        print(f"Recipe binaries unavailable ({status}); using pinned source builds", flush=True)
        return None
    receipt.write_text(json.dumps({"files": files, "repository": remote}, indent=2) + "\n")
    print("Resolved recipe binaries: " + str(repository), flush=True)
    return repository
