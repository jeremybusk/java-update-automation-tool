# Reviewed validation and publishing

The six stages use a retained run rather than replacing earlier state. `java-update.yml` contains commented defaults, and `repositories.yml` defines stable repository keys, application/group membership, requested refs, and dependencies. Paths in the portfolio resolve relative to that file. Workflow evidence paths resolve relative to the invocation directory.

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

A new invocation without `--resume` creates a separate run. `--from STAGE` reruns that stage and downstream requested stages in a saved run. Successful repository migrations and publishing target receipts are reused; failed migration attempts are kept in unique directories. To migrate new source content or change policy, create a new run.

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

## Validation contract

Stage 05 operates on migrated, committed source. It runs every discovered build root, obtains effective versions, checks every applicable pin and target, and creates fresh application/group alignment assessments. Missing or unresolved required versions fail. Report-only execution remains analyzed/prepared and cannot enter validation.

```yaml
workflow:
  validation:
    build: test                    # compile opts out of the test task explicitly
    timeout: 3600                  # Per-command timeout
    commands:
      - ["./scripts/contract-tests.sh"]
      - ["./scripts/integration-tests.sh", "--environment", "test"]
    repositories:
      orders-api:
        commands: [["./scripts/api-contract.sh"]]
```

Commands are argv lists, run from the migrated repository root; repository checks append to global checks. Configure required contract/integration checks explicitly. Commands that edit source fail validation. Command diagnostics retained here are sanitized outcomes; engine logs remain available under the migration attempt.

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

GitHub uses `GH_TOKEN`, falling back to `gh auth token`; GitLab uses `GITLAB_TOKEN`. Override the environment variable name with `token_env`. Tokens authenticate provider APIs and hosted HTTPS publication; SSH and existing Git credential helpers remain supported. Source discovery/history fetching uses normal Git credentials. Self-hosted GitLab can set `api_url: https://git.example.com/api/v4` and `namespace_id`. Tokens never belong in YAML or repository URLs. Saved URLs containing embedded credentials/query strings are rejected.

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

Normal tests use actual temporary Git histories and fake migration/build/provider boundaries, without external publication. Integration tests run real transformations, builds, effective inventories, and local publication. They skip without explicit opt-in or required tools. Use `portfolio.py --legacy` for previous four-stage invocations/state; legacy JSON is not imported as approved six-stage evidence.
