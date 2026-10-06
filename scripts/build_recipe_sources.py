#!/usr/bin/env python3
"""Warm the same recipe source cache used automatically by migrations."""
import argparse
import os
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yaml
from java_update_tool.recipe_sources import DEFAULT_CACHE, DEFAULT_LOCK, CODE_GENOME, SourceBuildError, prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("java-update.yml"))
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--java-home", type=Path, help="JDK 21 used to compile recipe sources")
    parser.add_argument("--env-file", type=Path, help="read credential assignments without executing shell code")
    args = parser.parse_args()
    rewrite = yaml.safe_load(args.config.read_text())["migration"]["openrewrite"]
    env = dict(os.environ)
    if args.java_home:
        env["JAVA_UPDATE_SOURCE_JAVA_HOME"] = str(args.java_home)
    keys = {rewrite.get("repository_username_env", "CODE_GENOME_USERNAME"),
            rewrite.get("repository_token_env", "CODE_GENOME_TOKEN")}
    if args.env_file:
        for line in args.env_file.expanduser().read_text().splitlines():
            match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
            if match and match[1] in keys:
                values = shlex.split(match[2], comments=True)
                if len(values) != 1:
                    raise SourceBuildError(f"invalid credential assignment for {match[1]}")
                env[match[1]] = values[0]
    options = dict(
        cache=args.cache or Path(os.environ.get("JAVA_UPDATE_RECIPE_CACHE", rewrite.get("source_cache", str(DEFAULT_CACHE)))),
        lock_path=Path(rewrite.get("source_lock", str(DEFAULT_LOCK))),
        remote=rewrite.get("artifact_repository") or CODE_GENOME,
        username_env=rewrite.get("repository_username_env", "CODE_GENOME_USERNAME"),
        token_env=rewrite.get("repository_token_env", "CODE_GENOME_TOKEN"), env=env)
    repository = None
    if rewrite.get("recipe_repository") == "auto":
        from java_update_tool.recipe_binaries import try_repository
        binary_options = dict(options, remote=rewrite.get("artifact_repository"))
        repository = try_repository(rewrite["artifacts"], **binary_options)
        if repository is not None:
            prepare([], **options)
    if repository is None:
        repository = prepare(rewrite["artifacts"], **options)
    print(f"Recipe Maven repository: {repository}")


if __name__ == "__main__":
    try:
        main()
    except (SourceBuildError, OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
