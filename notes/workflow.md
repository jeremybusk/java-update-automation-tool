# Reviewed validation and publishing

The six stages use a retained run rather than replacing earlier state. The obsolete `--legacy` and `--refresh` options have been removed. `java-update.yml` contains commented defaults, and `repositories.yml` defines stable repository keys, application/group membership, requested refs, and dependencies. Paths in the portfolio resolve relative to that file. Workflow evidence paths resolve relative to the invocation directory.

## Defaults and CLI overrides

Manual mode pauses after every stage, with artifact and Markdown locations printed before asking for approval. In a terminal, answer `yes` to proceed; otherwise a paused run returns 3. Review and approve in another invocation:

```bash
python3 portfolio.py run --all --through 06-publishing --execute
python3 portfolio.py approve RUN_ID --stage 01-discovery
python3 portfolio.py run --resume RUN_ID --all --through 06-publishing --execute
```

Repeat approval/resumption for each checkpoint. The checkpoint after validation gates entry to publishing, including remote creation. The publishing checkpoint reviews receipts after the operation. Choose fewer checkpoints or unattended mode deliberately:

```yaml
workflow:
  mode: manual
  checkpoints: [03-planning, 04-migration, 05-validation, 06-publishing]
  show_diffs: true
```

CLI overrides: `--mode manual|unattended`, `--[no-]show-diffs`, `--[no-]include-dependencies`, `--[no-]dependency-override`, `--[no-]enable-publishing`, and repeatable `--publish-target local_repo|src_repo|dst_repo`. They become part of the saved run; provide the same overrides on resume. `--execute` authorizes migration/publication execution and can be added after preparing commands.

A new invocation without `--resume` creates a separate run. `--from STAGE` reruns that stage and downstream requested stages in a saved run. A plain resume reuses successful repository migrations; explicitly restarting at migration or an earlier stage creates fresh migration attempts. Publishing reconciles existing target receipts. All migration attempts are kept in unique directories. To migrate new source content or change policy, create a new run. The report script reads retained runs using `--run-id` (default: `latest`).

## Retained resources and reviews

```text
.java-update/
  latest.json
  runs/<run-id>/
    run.json                        # Captured config, sources, stage state, approvals
    sources/<repository>/           # Exact clean input snapshots
    planned-policy.json             # Target-derived recipes and pinned artifacts
    01-discovery/.../result.json
    02-assessment/.../result.json
    03-planning/.../plan.json
    04-migration/repositories/<id>/
      result.json
      attempts/<attempt>/           # Policy, worktree, engine logs/reports
    05-validation/repositories/<id>/
      result.json                   # Exact validated commit/content fingerprint
      build-*/                      # Effective Maven/Gradle inventories
    05-validation/assessment/       # Fresh repository/cohort assessment
    06-publishing/repositories/<id>/result.json
    local-repositories/<id>/         # Durable local publishing target
    reports/<stage>/.../*.md
```

Approvals bind captured policy/source commits, stage JSON, execution policies, and migrated commit/content. Source snapshots must remain unchanged. Edited evidence invalidates affected approvals; changed validated output is blocked before publication. New policy/source inputs require a new run. Re-review and rerun validation after intentionally changing output; source changes must be committed before validation.

`runs` lists retained runs; `diff RUN_ID --compare-to OTHER_RUN_ID` creates a policy/source/output comparison. `show_diffs: true` generates comparisons with the previous retained run at checkpoints. Reports are generated views: regenerate with `portfolio.py report --run-id RUN_ID` or `scripts/render_reports.py --state .java-update --run-id RUN_ID`. Editing Markdown does not approve a stage.

Local Git inputs must be clean; requested local and remote refs are honored. Remote discovery snapshots are shallow. Before publication, fetch full required ancestry; validation is tied to the same migrated commit and does not rerun merely to expand history.

Discovery, migration, and validation share `discovery.max_depth` (default: 5).

## Dependencies, recipes, and pins

`depends_on` controls migration waves; cycles and ambiguous ID/name aliases fail before execution. Failed migrations block transitive dependents. Outside-selection dependencies require applicable evidence:

```yaml
workflow:
  include_dependencies: false
  dependency_evidence:
    common-library: .java-update/runs/VALIDATED_RUN/05-validation/repositories/common-library/result.json
  allow_dependency_override: false
```

Evidence must name the dependency, record successful validation, match target/alignment/migration/validation policy, and still describe the current dependency source commit and unmodified output. Alternatively enable `include_dependencies`. `allow_dependency_override` is available only in manual mode, records the experiment, and blocks publication.

Supported built-in targets are Java 11/17/21/25 and Spring Boot 3.5/4.0 lines. Spring Boot 3+ requires Java 17 or later. Recipes derive from the declared targets. Additional recipes remain available through `migration.openrewrite.recipes`; contradictory Boot upgrade recipes fail. Other Boot targets need an explicit reviewed mapping under `migration.openrewrite.target_recipes` and matching pinned recipe artifacts. Java-only portfolios omit framework recipes. A nonempty artifact list is completed with any missing Java/dependency/testing/cleanup packs; an explicitly pinned Spring artifact is required for framework migrations.

Pins under `alignment.dependencies.pins` use `group:artifact` or glob patterns. Exact keys take precedence. Conflicting matching glob values fail regardless of YAML order. Pins are checked against resolved dependencies even when only one repository contains them or all repositories agree on an incorrect version. Lower declared pins require `workflow.allow_downgrades: true`. This permits planning a downgrade; upgrade recipes may still require an explicit stack/property/custom recipe to achieve it. Unmet pins always fail validation. Managed/BOM upgrades are preferred over arbitrary managed-version overrides.

## Recipe sources and source builds

`migration.openrewrite` in `java-update.yml` is the global configuration for
recipe sources, remote Maven repositories, and the local source cache. The
checked-in default builds current recipe sources instead of downloading
customer-only MSAL JARs:

```yaml
migration:
  openrewrite:
    recipe_repository: source
    source_cache: ~/.cache/java-update/recipes
    source_lock: recipe-sources.lock.json
    artifact_repository: null
    repository_username_env: CODE_GENOME_USERNAME
    repository_token_env: CODE_GENOME_TOKEN
```

| Mode | Recipe artifacts | Remote repository when URL is null |
| --- | --- | --- |
| `source` | Build pinned GitHub source commits; reuse verified local JARs | Code Genome for Apache core/build dependencies |
| `auto` | Resolve binaries first; source fallback when recipe downloads fail | Maven Central for recipe probing; Code Genome for Apache build/core dependencies |
| `codegenome` | Download pinned current binaries | Code Genome; MSAL JARs need customer entitlement |
| `maven-central` | Download versions available on Central | Maven Central |
| `maven-local` | Use recipes already installed in Maven local | Maven Central for remaining dependencies |

Set `artifact_repository` to a Maven-compatible Nexus 3 group/hosted repository
URL to use your own server in `source`, `auto`, `codegenome`, or `maven-central` mode.
Configure credential **environment-variable names**, not values:

```yaml
artifact_repository: https://nexus.example/repository/maven-group/
repository_username_env: NEXUS_USERNAME
repository_token_env: NEXUS_PASSWORD
```

`source` and `auto` also build the pinned upstream Gradle plugin compatibility
fix against the same core. The published plugin predates a core marker API
change; using its old binary with current packs fails with `NoSuchMethodError`.
This source plugin stays in the private cache and is loaded before remotes.

`auto` checks the locked recipe dependency set before running any recipes. A
missing or denied POM/JAR triggers a source build, which is cached for reuse.
Recipe execution errors and failed application tests do not trigger fallback.
Map the chosen credential variables into your runner environment (or the
workflow's `env` section); an anonymous Nexus endpoint can leave them unset.

In source mode that endpoint must supply the Apache OpenRewrite build/core
artifacts. In binary mode it must also contain the requested recipe JARs and
POMs. A proxy cannot grant access to an upstream JAR that returns 403. A hosted
repository can hold internally built artifacts. Selecting Maven Central also
requires changing the artifact pins to versions it hosts; verified public pins
include spring `6.37.1`, migrate-java `3.42.1`, static-analysis `2.41.1`,
java-dependencies `1.60.2`, and testing-frameworks `3.44.0`. They include Boot
3.5/4.0 recipes, but are older than the current source pins.

A free Code Genome token is sufficient for source builds' Apache dependencies.
It does not grant access to prebuilt MSAL recipe JARs. See
[Code Genome's access requirements](https://docs.moderne.io/user-documentation/recipes/accessing-the-code-genome-project/).

Source builds run automatically before migration; report-only/dry runs do not
build recipes. To warm the cache explicitly with credentials from your local
`.env` (the file is parsed, never executed):

```bash
python3 -B scripts/build_recipe_sources.py --env-file ~/.env
```

The recipe compiler needs JDK 21, Python 3.12+, and access to GitHub, Gradle
Plugin Portal, Maven Central, and the configured Maven endpoint. Upstream Gradle
wrappers download the build Gradle version. For a Java 25 migration, retain JDK
25 on PATH and set `JAVA_UPDATE_SOURCE_JAVA_HOME` to a JDK 21 installation for
recipe compilation. Gradle plugin compilation also resolves its Android compile
API from Google's Maven repository. The warm-cache command accepts `--java-home PATH` and
`--cache PATH`. `JAVA_UPDATE_RECIPE_CACHE` overrides the configured cache path.
A Linux container with JDK 21 can serve the same purpose; mount a private
persistent cache volume rather than rebuilding an image for every migration.

`recipe-sources.lock.json` pins source commits, recipe dependency versions,
core version, and the build-plugin version. Update it together with recipe pins.
The set includes fourteen recipe packs and four Java-facing language APIs
needed by static analysis's compile-only checks, plus the compatible Gradle
plugin. Those APIs use their current
Java sources; native JavaScript/C#/Python/Go RPC backends are not packaged or
used by this Java migration runner.
The builder builds recipe dependencies first, adapts upstream settings to avoid
remote build caches/scans, disables release signing, and publishes the exact
locked coordinates into an isolated Maven repository. It retains license and
source-build modification notices in JARs; the older Joda commit's missing full
agreement is supplied from a pinned, checksum-verified upstream license copy.
Upstream recipe unit tests are not
run during packaging; the integration matrix validates the resulting migrations
and their application tests. Cache receipts hash the installed files; missing
or altered files rebuild their recipe. The cache key includes the lock file,
builder code, compiler JDK identity, and remote repository URL. Build logs are
kept inside the cache.

The four Boot checks share one hosted runner and private source cache. CI warms
recipes once, then runs at most two migration cases concurrently. Multiple
warm-cache commands using the same cache serialize behind a build lock.
For planning, allow 15–30+ minutes for cold recipe
preparation, plus migration and application tests; this estimate has not been
measured on a fresh GitHub Actions runner.

To reduce repeated preparation, retain a private cache on a persistent runner
or mounted volume. When adapting the workflow to a persistent runner, set the
repository variable `JAVA_UPDATE_RECIPE_CACHE` to that volume's cache path.
Alternatively, populate an authenticated internal Nexus
repository with source-built recipes, then select `auto` and its
`artifact_repository` URL to reuse those binaries. Subagents can investigate
failures and review tests while builds run; compilation still follows the
recipe dependency order.

MSAL permits internal use subject to its limitations and notice requirements;
see the [license terms](https://docs.moderne.io/licensing/moderne-source-available-license/).
Keep compiled MSAL recipes in a private runner cache or authenticated internal
hosted repository. Public CI uploads redacted source-build logs, while compiled
recipes stay out of Actions artifacts and Gradle caches. Hosted runners rebuild
on a fresh job; persistent internal runners
or a private cache volume retain builds across runs. Docker changes the build
environment, not artifact entitlement or distribution terms. See also the
[upstream source-build instructions](https://docs.openrewrite.org/reference/building-openrewrite-from-source).

## Validation contract

Stage 05 operates on migrated, committed source. It runs every discovered build root, obtains effective versions, checks every applicable pin and target, and creates fresh application/group alignment assessments. Missing or unresolved required versions fail. Report-only execution remains analyzed/prepared and cannot enter validation.

```yaml
workflow:
  validation:
    build: test                    # compile needs a reasoned unit exemption
    timeout: 3600                  # Per-command timeout
    commands:
      - ["./scripts/contract-tests.sh"]
      - ["./scripts/integration-tests.sh", "--environment", "test"]
    repositories:
      orders-api:
        commands: [["./scripts/api-contract.sh"]]
```

Commands are argv lists, run from the migrated repository root; repository checks append to global checks. Configure required contract/integration suites and fresh report globs explicitly. Commands that edit source fail validation. Bounded redacted diagnostics are retained under the run, with locations in stage resources and summaries.

Maven validation runs `test`/`compile`, `help:effective-pom`, and resolved `dependency:tree` inventories, including reactor modules. Gradle runs `test`/`classes` and an init-script task that resolves build configurations, including BOM/catalog dependencies. Effective Java compiler levels/toolchains and resolved Boot versions govern compliance. A dependency inventory that cannot be resolved blocks publication.

By default any validation or cohort failure stops the run before publishing. With `independent_applications: true`, only successful applications whose applicable application/group cohorts are complete and aligned, and whose dependencies are validated, can publish. Failed/blocked work stays recorded and the invocation returns failure. Manual mode still asks for approval of the partial validation result.

## Publishing and history

```yaml
workflow:
  publishing:
    enabled: true
    targets: [local_repo, src_repo, dst_repo]
    history: default              # all includes source branches and tags
    provider: github              # gitlab also supported
    mode: autocreate
    owner: acme
    prefix: java25-
    default_branch: main
    source_branch: automation/java-{java}
    private: true
    repositories:
      specially-named-service:
        mode: precreated
        name: existing-modern-service
```

`local_repo` is the default: a retained Git checkout with the migrated commit and full ancestry, under `local-repositories/`. `src_repo` pushes a migration branch, leaving the source default branch in place. `dst_repo` pushes approved migrated code to the configured destination default branch, normally `main`. Source `master` can map to destination `main` without changing commit ancestry.

`history: default` transfers the validated branch ancestry. `history: all` adds source branches/tags; the source default branch maps to the destination name. Conflicting branch mappings, divergent destination branches, or different existing tag values block the target. Pushes are atomic, use explicit refs, and never imply force updates or deletions. Complete history transfer is not a destructive `git push --mirror`.

For hosted source default-branch changes, explicitly set `source_default_branch: main`. The original default tip must still match the captured tip. Its history is copied to the new source branch and the provider default is changed; the old branch remains. This is separate from the source migration branch and requires host API permissions.

Auto-creation requires an owner and prefix (or an explicit per-repository name); a name collision stops. A failed push after creation retains that creation receipt, allowing retry without treating the same run's destination as a collision. To reuse an existing repository, select `precreated` with owner/name, or a per-repository Git `url`. Populated repositories must pass ancestry checks. Use owner/name for hosted destinations when their default-branch setting must be changed through the API; a plain Git URL publishes the configured ref but does not alter host settings.

GitHub uses `GH_TOKEN`, falling back to `gh auth token`; GitLab uses `GITLAB_TOKEN`. Override the environment variable name with `token_env`. Tokens authenticate provider APIs and hosted HTTPS publication; SSH and existing Git credential helpers remain supported. Source discovery/history fetching uses separately configured host credentials or normal Git credential helpers. Self-hosted GitLab can set `api_url: https://git.example.com/api/v4` and `namespace_id`. Tokens never belong in YAML or repository URLs. Saved URLs containing embedded credentials/query strings are rejected.

Publishing receipts are independent per target. After partial success, return code 1, retain the validated commit and successful receipts, and resume publishing to retry only unfinished targets:

```bash
python3 portfolio.py run --resume RUN_ID --from 06-publishing --through 06-publishing --execute
```

Successful pushes are not rolled back automatically. Changing target configuration requires a new run.

## Exit codes and tests

| Code | Meaning |
| --- | --- |
| 0 | Requested stages succeeded, or migration commands were prepared/analyzed. |
| 1 | Migration, validation, or publishing failed/blocked; inspect JSON/receipts. |
| 2 | Invalid inputs, changed evidence, missing prerequisites, or command/config errors. |
| 3 | Manual review is pending; approve and resume the retained run. |

```bash
python3 -B -m unittest discover -s tests -v
# Opt-in real migrations: JDK 21+, Maven and/or compatible Gradle, network access.
JAVA_UPDATE_INTEGRATION=1 python3 -B -m unittest discover -s tests -p test_integration.py -v
```

Normal tests use actual temporary Git histories and fake migration/build/provider boundaries, without external publication. Integration tests run real transformations, builds, effective inventories, and local publication. They skip without explicit opt-in; once enabled, missing tools or credentials fail.
# Hardened validation and retained-run operations

Omitted Git refs select the source default branch consistently. Local repositories
use `origin/HEAD` when available; without it, a checked-out `main`/`master`, then
the available conventional default, then the sole/current local branch resolves
the default. Configure an explicit ref when a repository has a different intended
default. `workflow.source_selection: current` or `--source-selection current`
explicitly selects a local checkout. Dirty local input remains an error.

Snapshots persist independently per repository. An interrupted clone is rebuilt;
a successfully promoted snapshot is reconciled and verified before resumption.
Rejected resume/approval invocations appear in `events.jsonl` without changing a
previously completed outcome. Invocation errors return 2, stage failures return 1,
and manual review pauses return 3.

Before review, a run captures the history-transfer manifest and request base SHA.
Publishing obtains full ancestry for those captured objects. A changed manifest
requires a new run. Source migration branches contain the run ID and remain stable
across retries; older branch templates receive a run suffix automatically.
Target receipts bind the validated SHA, tree, destination, and history plan.
Retries reconcile actual refs. Human edits, divergent refs, and tag conflicts
stop an update; successful targets remain independently recorded.

Validation discovers independent nested builds and checks all of them by default.
Only declared Maven modules/Gradle projects establish parent membership.
Composite Gradle builds remain independent checks. Dynamic membership that cannot
be established statically is treated conservatively as an independent build.
Use `validation.build_roots` to select a narrower scope and `validation.exclusions`
to give a reason for every excluded root. Paths are relative to the migrated repo;
the root is `.`. Reports disclose selected scope and exclusions.

```yaml
workflow:
  validation:
    build_roots: ["."]
    exclusions:
      tools/old-service: "Separate application handled in a later migration"
    test_exemptions: {} # e.g. {unit: "A generated BOM with no executable code"}
    suites:
      - name: contract
        command: ["./scripts/run-contract-tests"]
        reports: ["build/contract-results/TEST-*.xml"]
```

Fresh executed unit tests are required for publication. Validation deletes prior
generated XML reports, disables Gradle build-cache/test-task reuse, and records
counts and report hashes. Missing, empty, entirely skipped, or failing suites do
not qualify. Compile-only checks require a reasoned unit exemption to publish a
legitimately testless project. Declared Maven Failsafe checks run through `verify`;
declared Gradle integration/contract tasks and configured suites also need fresh
reports. Use configured suites for nonstandard task/report conventions. Report
exemptions and exclusions are part of the validation policy and approvals.
Custom suites retain separate evidence from automatically detected suites, even
when their names match.

Maven compliance follows the effective compiler release/target configuration and
executions. Informational `java.version` values are recorded separately. Resolved
dependencies, framework versions, dependency evidence, and cohort gates remain
required. Older evidence without the new evidence version must be refreshed.

Optional draft reviews are disabled by default:

```yaml
workflow:
  publishing:
    enabled: true
    targets: [local_repo, src_repo]
    request:
      enabled: true
      base: null # capture source default; alternatively a specific source branch
      links: []  # optional HTTPS links to hosted CI/artifact evidence
  source_credentials:
    github.com: GH_TOKEN
    gitlab.example.org: SOURCE_GITLAB_TOKEN
```

Draft GitHub PRs and GitLab MRs are created after source-branch publishing. Their
receipts are separate: retrying an API failure reconciles the existing matching
request and skips an unchanged successful push. The base tip must still match the
captured commit before creation. Requests summarize the validated commit, target,
checks, test counts, exclusions, exemptions, and run identity. Human title/body
edits and readiness are preserved. Closed/merged or mismatching requests require
explicit operator resolution; automation never reopens, downgrades, or replaces
them. API permissions remain the hosting provider's normal requirements.

Source credentials are selected by intended host independently from destination
credentials. Provider tokens are used only on matching Git/API hosts; unmatched
remotes use their normal credential helpers. Persist environment-variable names
in YAML, with token values supplied only through the environment.

`portfolio.py export-evidence RUN_ID --output /path/evidence.tar.gz` creates a
redacted inspection bundle with SHA-256 file manifest. Hosted links are optional.
The bundle includes outcomes, inventories, approvals/receipts, reports, and retained
diagnostics; it does not provide portable resumption of host-specific checkouts.

Every run has `events.jsonl`, `tool-versions.json`, and `diagnostics/`. Commands
record bounded redacted output, duration, status, and diagnostic locations. Defaults
are 10 MiB/check, 100 MiB/run, and 30 days for diagnostic logs, configurable through
`workflow.diagnostics`. Truncation is explicit. Log expiry leaves durable
inventories, approvals, outcomes, and publication receipts intact.

Local locks reject concurrent mutation of the same run or destination immediately
with owner information. Independent runs can run concurrently. Use one host per
state directory, isolated CI job state, CI target concurrency controls, and Git
remote-ref guards for other writers. The event journal is an operational trace.

`portfolio.py prune` is a dry run. `portfolio.py prune --apply` applies eligible
run deletion. Defaults protect the latest run; active, pending, failed/retryable
runs; referenced evidence/output; and durable local publishing repositories.
`workflow.retention.days` defaults to 30. Add run IDs or evidence paths to
`workflow.retention.pins` for references outside the state directory. Only
diagnostic expiration is automatic. Deletion decisions are retained in the state
journal. Old attempt policies required for retained checkpoint identities remain
protected with their run.

## Required CI matrix

The `Migration checks` workflow runs fast regressions for every PR. Changes to
migration, validation, shared workflow/discovery/policy code, tests, or CI require
real builds. Releases and manual checks run the entire matrix. Require the
`migration-gate` check in branch protection and publish releases only after its
successful tag/manual run. Repository protection settings are managed separately
from these checked-in workflows.

| Case | Input → target | Tool/layout |
| --- | --- | --- |
| maven17, gradle17 | Java 17 → 21 | Single project |
| maven8, gradle8 | Java 8 → 25 | Single project |
| maven-bom | Java 17 → 21 | Versionless JUnit dependency via BOM |
| maven-multi, gradle-multi | Java 17 → 21 | Maven reactor / Gradle multi-project |
| gradle-catalog | Java 17 → 21 | Dependency version catalog |
| boot35-maven, boot35-gradle | Boot 3.4.2 → 3.5.x; Java 17 → 21 | Framework recipes |
| boot4-maven, boot4-gradle | Boot 3.5.1 → 4.0.x; Java 17 → 21 | Framework recipes |

CI caches pip downloads, the checksum-verified Maven 3.9.11 archive, and Maven
dependencies for Java-only cases. Native jobs install only their required build
tool; Gradle jobs retain their existing dependency cache. Boot jobs keep compiled
recipes and Gradle caches private to the runner. Superseded pull-request runs
are cancelled automatically. Integration timings,
including recipe preparation, appear in the job summary. Tests still execute
freshly; cached build output cannot qualify as validation evidence.

CI uses Maven 3.9.11, Gradle 8.14.3 with JDK 21 for Java-21/Boot cases, and Gradle
9.1.0 with JDK 25 for Java-25 cases. Recipe versions are pinned in the migration
defaults; Boot uses `rewrite-spring:6.40.0` built from its locked source commit. Each run records actual tool versions.
These fixtures establish this matrix, rather than universal tool/version support.

Boot cases use the global recipe configuration. With source defaults, trusted
jobs need `CODE_GENOME_USERNAME` and `CODE_GENOME_TOKEN` secrets for Apache
build dependencies. Java-only baseline cases use Maven Central and run on fork
PRs. Maintainers validate fork changes on a trusted repository branch before
merging; required skipped or failed checks fail the gate. The workflow never
uses `pull_request_target` to execute fork content with secrets.

Run a selected native case locally with JDK and build tools available:

```sh
JAVA_UPDATE_INTEGRATION=1 JAVA_UPDATE_CASE=maven8 \
  JAVA_UPDATE_ARTIFACTS=/tmp/java-update-evidence \
  python3 -m unittest discover -s tests -p test_integration.py -v
```

An enabled native check fails for missing prerequisites rather than reporting
successful coverage. Without `JAVA_UPDATE_INTEGRATION=1`, the normal regression
suite skips its explicit native-test boundary.
