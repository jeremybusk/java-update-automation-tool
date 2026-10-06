# Java repository update portfolio

Inspect, migrate, validate, and publish related Maven and Gradle repositories through six retained stages. Root YAML sets global defaults; CLI options override one run. Manual mode pauses after every stage and prints its resources before approval. Unattended mode uses the same validation gates.

```bash
python3 -m pip install -r requirements.txt
python3 portfolio.py validate
python3 portfolio.py run --application legacy-catalog
```

The checked-in portfolio contains local Java 8 fixtures and pinned public demo codebases. Start with `--application legacy-catalog` for the local fixtures or `--repo spring-petclinic` for a Spring Boot application with Maven and Gradle builds. See [demo repositories and recipe references](examples/demo-repositories.md) for baselines, commands, and prerequisites. An unfiltered run selects the entire portfolio, including the large Baeldung tutorials collection.

A default run requests review after discovery and stops at planning once its checkpoints are approved. Migration requires `--execute`; the default publishing target is an inspectable local Git repository.

## Stages

| Stage | Result |
| --- | --- |
| `01-discovery` | Capture exact source commits; inspect build roots and declared versions. |
| `02-assessment` | Evaluate target policy and dependency alignment across complete cohorts. |
| `03-planning` | Select target recipes, resolve pins, and produce dependency waves. |
| `04-migration` | Prepare commands, or execute OpenRewrite and commit transformed code. |
| `05-validation` | Build/test migrated code, run custom checks, resolve effective dependencies, and reassess compliance. |
| `06-publishing` | Preserve full ancestry and publish the exact validated commit to selected targets. |

YAML/JSON are the source of truth; generated Markdown provides review summaries. Each run lives in `.java-update/runs/<run-id>/` with its own snapshots, migration attempts, approvals, validation evidence, reports, and publishing receipts.

## Review and resume

```bash
# Noninteractive manual runs pause with exit code 3.
python3 portfolio.py approve latest --stage 01-discovery
python3 portfolio.py run --resume latest --application legacy-catalog

# Continue through validation after approving the intermediate checkpoints.
python3 portfolio.py run --resume latest --through 05-validation --execute

# Inspect the validated result, approve validation, and materialize local output.
python3 portfolio.py approve latest --stage 05-validation
python3 portfolio.py run --resume latest --through 06-publishing --execute
```

Use the same config, repository definitions, selection, and CLI overrides when resuming. Source/policy changes require a new run; edited reviewed artifacts invalidate downstream approval. `--from` deliberately reruns a stage in an explicitly resumed run.

```bash
python3 portfolio.py run --mode unattended --all --through 06-publishing --execute
python3 portfolio.py runs
python3 portfolio.py diff RUN_ID --compare-to PREVIOUS_RUN_ID
python3 portfolio.py report --run-id latest
python3 portfolio.py export-evidence RUN_ID --output evidence.tar.gz
python3 portfolio.py prune                   # Show retention and protected resources
python3 portfolio.py prune --apply           # Delete only eligible retained runs
```

Remote publishing also requires `workflow.publishing.enabled: true` and a selected remote target. Nothing is pushed merely by running migration or validation.

Validation requires fresh executed tests and effective compiler/dependency evidence. Independent nested builds are included unless explicitly excluded with a reason. Optional draft GitHub/GitLab requests summarize the validated scope after source-branch publication. Captured refs, exclusive locks, exact publishing receipts, redacted diagnostics, and an operational journal make interrupted runs inspectable and retryable.

## Configuration

[`java-update.yml`](java-update.yml) includes commented global options for manual checkpoints, diffs, dependency evidence, validation commands, publishing targets, GitHub/GitLab destinations, and history scope. [`repositories.yml`](repositories.yml) associates repository IDs/names with applications and groups and declares `depends_on` relationships.

Use `--repo`, `--application`, `--application-group`, or `--all` to select scope. Dependency waves block dependents after migration failure. Dependencies outside the selection need matching validation evidence; `--include-dependencies` includes them recursively. A manual experiment override is recorded and blocks publication.

The checked-in policy targets Java 25 and Spring Boot 4.0.x. Its global `migration.openrewrite` configuration defaults to building pinned recipe sources and reusing a private local cache. It also supports `auto` (binary downloads with source fallback), Maven Central, Code Genome, Maven local, and a Nexus Maven repository URL. Source builds need JDK 21 and a free Code Genome token for Apache build dependencies; prebuilt MSAL recipe downloads require customer entitlement. Target recipes are selected automatically; explicit additions must agree with targets. See [recipe sources, caching, and repository settings](notes/workflow.md#recipe-sources-and-source-builds).

See [the workflow guide](notes/workflow.md) for complete config examples, credentials, validation behavior, retry semantics, integration tests, and exit codes. Editor schemas live in [`schemas/`](schemas/).

## Tests and low-level engine

```bash
python3 -B -m unittest discover -s tests -v
```

Normal tests use temporary Git repositories, deterministic migration/build boundaries, and local fake hosting APIs. The opt-in integration matrix covers Maven/Gradle Java 17→21 and 8→25, BOMs, multi-project builds, catalogs, and Spring Boot 3.4→3.5→4.0. Boot checks use the global recipe configuration and source builds on trusted jobs; the Java-only baseline uses Maven Central. Missing prerequisites fail required checks. See the workflow guide for the precise fixture and toolchain matrix.

`migrate.py` remains available for direct OpenRewrite execution. `portfolio.py` uses the six-stage workflow exclusively. The reusable [report skill](skills/java-update-reports/SKILL.md) regenerates Markdown from retained JSON.
