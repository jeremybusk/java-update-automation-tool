# Java repository update portfolio

This repository investigates and updates related Maven and Gradle Git repositories as a four-stage, rerunnable workflow. Define repositories once in YAML, select one repository, an application, or an application group, and retain every machine-readable artifact needed by the next stage.

YAML configuration and generated JSON are the source of truth. Markdown reports are generated views with review checklists.

## Quick start

The checked-in [`repositories.yml`](repositories.yml) points at the two local example projects.

```bash
python3 -m pip install -r requirements.txt
python3 portfolio.py validate
python3 portfolio.py run --application legacy-catalog
```

The default run safely stops after planning. Its output is under `.java-update/`:

```text
.java-update/
├── 01-discovery/
│   ├── repositories/<repo>/result.json
│   ├── applications/<application>/result.json
│   └── application-groups/<group>/result.json
├── 02-assessment/
│   ├── repositories/<repo>/result.json
│   ├── applications/<application>/result.json
│   └── application-groups/<group>/result.json
├── 03-planning/
│   ├── repositories/<repo>/plan.json
│   ├── applications/<application>/plan.json
│   └── application-groups/<group>/plan.json
├── 04-migration/
│   ├── repositories/<repo>/
│       ├── migration-policy.json
│       └── result.json
│   ├── applications/<application>/result.json
│   └── application-groups/<group>/result.json
├── reports/                 # disposable Markdown views
├── repositories/            # managed remote discovery checkouts
└── runs/                     # invocation receipts
```

## Portfolio YAML

Each repository needs only a name, source, application association, and application-group association. `repo_id` is optional and becomes the stable key when present. `depends_on` controls migration wave ordering.

```yaml
schema_version: 1
repositories:
  - repo_name: orders-api
    repo_id: orders-api-prod
    source: git@github.com:acme/orders-api.git
    ref: main
    application_id: orders
    application_group_id: commerce
    role: api

  - repo_name: orders-worker
    source: git@github.com:acme/orders-worker.git
    application_id: orders
    application_group_id: commerce
    role: worker
    depends_on: [orders-api-prod]
```

Local sources are resolved relative to the portfolio file. HTTPS, SSH, and Git URLs are supported. Git credentials follow the normal Git credential/SSH configuration used by the existing migration engine.

The format is documented by [`schemas/repositories.schema.json`](schemas/repositories.schema.json).

## Root update policy

[`java-update.yml`](java-update.yml) owns desired and acceptable Java/Spring Boot versions, dependency families that must remain aligned, optional exact dependency pins, discovery behavior, migration verification, and OpenRewrite recipes.

The checked-in defaults target Java 25 and the Spring Boot 4.0.x line. Java 21/25 and Spring Boot 3.5.x/4.0.x are accepted during staged rollout. These are policy choices, not constants in the Python code—review them for your runtime, vendor support, and platform BOMs.

For alignment, a dependency is compared only when it occurs in two or more selected related repositories. Put exact organization decisions under `alignment.dependencies.pins`; when mismatched libraries lack a pin, the generated plan deliberately records `decision-required` instead of guessing a version.

The policy format is documented by [`schemas/java-update.schema.json`](schemas/java-update.schema.json).

## Four stages

1. `01-discovery` inspects Git state, Maven/Gradle roots, Java versions, Spring Boot versions, direct dependencies, features, and external build configuration.
2. `02-assessment` evaluates desired/acceptable targets and compares versions across complete application and application-group cohorts.
3. `03-planning` produces repository and cohort migration plans, explicit pending tasks, completion rules, and dependency-ordered waves.
4. `04-migration` materializes the exact migration policy and command. It executes only when `--execute` is supplied, then retains the existing OpenRewrite engine's worktree, logs, and reports.

Every stage can be rerun from its upstream JSON:

```bash
# Refresh source and recreate discovery through planning.
python3 portfolio.py run --application orders

# Reassess and replan without touching source checkouts.
python3 portfolio.py run --application orders \
  --from 02-assessment --through 03-planning

# Prepare stage 04 without executing OpenRewrite.
python3 portfolio.py run --repo orders-api-prod \
  --from 04-migration --through 04-migration

# Execute after reviewing the plan and generated policy.
python3 portfolio.py run --application orders \
  --from 04-migration --through 04-migration --execute
```

Running a single repository is intentionally valid, but its application/group assessment is marked `incomplete-cohort`. Use application or group scope for a final alignment decision.

## Selectors

Use one selector kind per run; each option is repeatable.

```bash
python3 portfolio.py run --repo orders-api-prod
python3 portfolio.py run --application orders
python3 portfolio.py run --application-group commerce
python3 portfolio.py run --all
```

With no selector, `--all` is implied.

## Reports and checklists

Runs automatically regenerate Markdown. You can regenerate it at any time without cloning repositories or changing JSON:

```bash
python3 portfolio.py report --application-group commerce
python3 scripts/render_reports.py --state .java-update
```

The reusable [`java-update-reports` skill](skills/java-update-reports/SKILL.md) tells an agent how to regenerate and interpret reports without treating Markdown as state.

## Migration credentials and recipes

The default Spring Boot 4.0 recipe is distributed through Code Genome and requires:

```bash
export CODE_GENOME_USERNAME='you@example.com'
export CODE_GENOME_TOKEN='your-download-token'
```

It is configured in `java-update.yml` alongside the full recipe artifact set because the legacy migration engine treats a non-empty artifact list as an override. For Java-only, account-free migrations, clear `migration.openrewrite.recipes` and `artifacts`, then set `recipe_repository: maven-central`.

Stage 04 delegates repository transformation to the existing [`migrate.py`](migrate.py) engine. That engine works on a copied/cloned worktree, runs OpenRewrite phases, verifies builds, and retains detailed JSON diagnostics. It does not edit local source inputs.

## Validation and tests

```bash
python3 portfolio.py validate
python3 -m unittest discover -s tests -v
```

JSON schemas help editors and CI validate structure. Runtime validation additionally enforces unique repository keys/names, valid dependency references, and one application group per application.

## Legacy low-level CLI

Use `migrate.py` directly when you already know the exact repository and migration policy and do not need portfolio association, consistency assessment, or staged plans:

```bash
python3 migrate.py ./service --target-java 25 --profile standard --force
```

See [`migration-policy.example.yml`](migration-policy.example.yml) for its detailed OpenRewrite policy fields.
