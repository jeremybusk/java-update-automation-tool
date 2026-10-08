# Modernize one repository

`modernize.py` accepts a source, an existing destination, and a destination branch
as arguments. It reuses the six-stage engine, including fresh test evidence,
policy checks, preserved Git ancestry, and retryable publishing. You do not need
to create or edit `repositories.yml`.

## Clone and run from anywhere

Clone the whole tool: this small entry point imports the shared engine and uses
its shipped policy and pinned recipe sources.

```bash
git clone --branch dev https://github.com/jeremybusk/java-update-automation-tool.git "$HOME/tools/java-update"
python3 -m venv "$HOME/tools/java-update/.venv"
"$HOME/tools/java-update/.venv/bin/python" -m pip install -r "$HOME/tools/java-update/requirements.txt"

# Run from a working directory of your choice.
"$HOME/tools/java-update/.venv/bin/python" "$HOME/tools/java-update/simple-cli-apps/modernize.py" \
  --source git@github.com:acme/legacy-app.git \
  --destination git@github.com:acme/modernized-app.git \
  --branch modernized/java-25 \
  --java 25
```

This command captures the source and produces an assessment and plan. Add
`--execute` to migrate, validate, and publish to the specified branch. Without
`--execute`, it does not migrate code or push any branch. Planning still needs
read access to the source.

Use Python 3.12+, Git, the target JDK on `PATH`, and the application's Maven or
Gradle wrapper (or installed build tool). The shipped policy targets Java 25 and
Spring Boot 4.0.x. Its recipe source builds also need JDK 21 and Code Genome
credentials:

```bash
export JAVA_UPDATE_SOURCE_JAVA_HOME=/path/to/jdk-21
export CODE_GENOME_USERNAME=your-username
export CODE_GENOME_TOKEN=your-token
```

Source recipes build automatically and reuse a private cache. See
[recipe setup and alternate repositories](../notes/workflow.md#recipe-sources-and-source-builds)
for cache warming, network requirements, and alternate recipe modes.

SSH uses your normal Git authentication. HTTPS uses Git credentials or `GH_TOKEN`
on GitHub / `GITLAB_TOKEN` on GitLab. Set `--provider gitlab` for a GitLab HTTPS
destination. Tokens belong in the environment, never in URL arguments.

## Arguments

| Argument | Meaning |
| --- | --- |
| `--source LOCATION` | Required Git URL or clean local source directory. |
| `--destination LOCATION` | Required existing Git URL or local bare repository. |
| `--branch NAME` | Required destination branch; also used for retained local output. |
| `--source-ref REF` | Source branch, tag, or commit; default: source default branch. |
| `--java 11\|17\|21\|25` | Override the Java target and require that version at validation. |
| `--boot VERSION` | Override the Spring Boot target, e.g. `4.0.x`; affects Boot apps only. |
| `--profile PROFILE` | `conservative`, `standard`, or `aggressive`; default: shipped policy. |
| `--provider PROVIDER` | `github` or `gitlab`; default: shipped policy. |
| `--state PATH` | Run storage; default: `.java-update` in the calling directory. |
| `--config PATH` | Optional advanced policy; default: the tool's `java-update.yml`. |
| `--execute` | Run all six stages and publish when validation succeeds. |
| `--local-only` | With `--execute`, publish only to the retained local checkout. |
| `--resume RUN_ID` | Retry a retained run using the same arguments; accepts `latest`. |

Source and destination locations are supplied as arguments even with an optional
policy file. Relative paths resolve from your calling directory; recipe lock and
cache paths in an optional policy resolve relative to that policy file.
Per-repository validation overrides in advanced policies use the stable ID `app`.

## Output and retries

Automatically generated inputs live in `.java-update/inputs/`; snapshots, plans,
logs, validation results, and publishing receipts live in
`.java-update/runs/<run-id>/`. Local output is retained under
`<run-id>/local-repositories/app`.

Rerun an executed command with the same arguments plus `--resume RUN_ID` to retry
it. Include `--execute` again. Changing source, targets, destination, or branch
requires a new run. To try migration without a remote push, add
`--execute --local-only`. To inspect retained results:

```bash
"$HOME/tools/java-update/.venv/bin/python" "$HOME/tools/java-update/portfolio.py" \
  --state /path/to/working-directory/.java-update report --run-id latest
```

The destination can already contain code on other branches. A new destination
branch receives the validated source tree and its ancestry; this does not merge
in destination-only files. An existing target branch must allow a fast-forward;
divergent branches are rejected. Use a new branch for independent modernization
runs. Explicit destinations leave the host's default branch unchanged, and this
command does not push back to the source or create a PR.

A successful command exits `0`; a failed workflow exits `1`; invalid inputs exit
`2`. Failed tests or policy checks block publication.
