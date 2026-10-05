# Demo repositories and recipe references

[`repositories.yml`](../repositories.yml) includes the local Java 8 fixtures and
four public application codebases. Public entries use exact older commits so
upstream upgrades do not remove the changes you want to exercise. Each public
codebase has its own application and group, allowing a single-repository run to
form a complete validation cohort. Copy an entry into your own portfolio, or
select its `repo_name` with `--repo`.
The same public refs are available in
[`repositories.example.txt`](../repositories.example.txt) for `migrate.py` manifests.

## Application targets

| Portfolio selector | Baseline | Useful coverage |
| --- | --- | --- |
| `--application legacy-catalog` | Local Java 8 / JUnit 4 fixtures | Independent Maven and Gradle builds, Java/compiler/plugin upgrades, test migration, dependency updates, and dependency waves. |
| `--repo spring-petclinic` | [Petclinic commit](https://github.com/spring-projects/spring-petclinic/tree/6148ddd9671ccab86a3f0ae2dfa77d833b713ee8): Java 17, Boot 3.4.0 | Baseline Spring Boot upgrades, JPA/Hibernate, static analysis, and both Maven and Gradle builds. `discovery.build_tool: auto` discovers both build files at the root. |
| `--repo spring-petclinic-microservices` | [Microservices v3.4.1](https://github.com/spring-petclinic/spring-petclinic-microservices/tree/f9fd559361f11b79e622ee0c0c660f42980a36ac): Java 17, Boot 3.4.1, Cloud 2024.0.0 | Maven reactor, service modules, Cloud BOM alignment, Gateway APIs, and coordinated framework changes. |
| `--repo cargotracker` | [Cargo Tracker EE 8 commit](https://github.com/eclipse-ee4j/cargotracker/tree/e6b8648cda2754aee39cb536c807b051cab74b61): Java 8, Jakarta EE 8 / `javax`, Payara 5 | Enterprise Java, JPA, WAR packaging, and explicit EE namespace migration. The official repository is `eclipse-ee4j/cargotracker`. |
| `--repo baeldung-tutorials` | [Tutorials January 2025 commit](https://github.com/eugenp/tutorials/tree/346395d72eab1d59bd6052d23ee6a4e9c36dc24a): mixed Java/framework/library versions | Diverse recipe inputs, including `guava-modules`, `gradle-modules`, and many independent Maven projects. |

These are migration inputs, not a guarantee that every application will pass the
default policy without further configuration. The checked-in policy targets
Java 25 and Boot 4.0.x; namespace, application-server, and organization-specific
changes require additional reviewed recipes and validation.

## Example runs

Run commands from this tool's repository root. Install the Python requirements,
then validate the configuration and try the small local portfolio:

```bash
python3 -m pip install -r requirements.txt
python3 portfolio.py validate
python3 portfolio.py run --mode unattended --application legacy-catalog
```

Prepare a public Spring Boot migration through stage 04. This clones the pinned
source, discovers both build tools, assesses targets, and saves migration commands
without executing recipes or builds:

```bash
python3 portfolio.py run --mode unattended --repo spring-petclinic --through 04-migration
```

Execute and validate a new Petclinic run after configuring the toolchain and
recipe credentials:

```bash
python3 portfolio.py run --mode unattended --repo spring-petclinic --through 05-validation --execute
```

Execution needs a full JDK supporting the target (JDK 25 for the checked-in
policy), Maven and compatible Gradle tooling/wrappers, network access, and
`CODE_GENOME_USERNAME` / `CODE_GENOME_TOKEN`. Petclinic also contains database
integration tests using Testcontainers/Docker Compose; provide Docker for those
suites. Validation checks both discovered build tools. With manual review,
replace `--mode unattended` with `--mode manual` and follow the printed
approve/resume commands. Outputs and reports are retained under
`.java-update/runs/<run-id>/`; stage 05 stops before publication.

Inventory and plan the larger examples individually:

```bash
python3 portfolio.py run --mode unattended --repo spring-petclinic-microservices
python3 portfolio.py run --mode unattended --repo cargotracker
python3 portfolio.py run --mode unattended --repo baeldung-tutorials
```

`--all` and an omitted selector include every entry, including Tutorials. Prefer
the explicit selectors above for a first run.

## Choosing additional recipes and scope

Copy `java-update.yml` to a separate configuration when experimenting with extra
recipes; retain its pinned artifacts and replace the empty
`migration.openrewrite.recipes` list. For Cargo Tracker's EE 8 namespace migration,
one starting point is the [Jakarta EE 9 migration recipe](https://docs.openrewrite.org/recipes/java/migrate/jakarta/javaxmigrationtojakarta):

```yaml
# Within migration.openrewrite in java-update.jakarta.yml:
recipes:
  - org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta
```

```bash
cp java-update.yml java-update.jakarta.yml
# Edit the recipes list above before preparing this run.
python3 portfolio.py --config java-update.jakarta.yml run --mode unattended --repo cargotracker --through 04-migration
```

Review the Payara/application-server target and Arquillian tests alongside the
namespace change. A Java upgrade alone does not migrate the EE platform or its
runtime. For Microservices, review the Cloud BOM and Gateway migration with the
Boot upgrade; [Spring Cloud 2025.1 recipes](https://docs.openrewrite.org/recipes/java/spring/cloud2025/upgradespringcloud_2025_1)
describe the Cloud line compatible with Boot 4.0.

For Tutorials, begin with discovery and choose a small buildable fixture for
execution. `--repo baeldung-tutorials` selects the entire repository; it does not
limit migration to `guava-modules`. Preserve parent POMs, settings, and required
modules when preparing a smaller local input. The
[Guava-to-standard-library recipe](https://docs.openrewrite.org/recipes/java/migrate/guava/noguava)
is `org.openrewrite.java.migrate.guava.NoGuava`, supplied by `rewrite-migrate-java`.
The collection also includes examples needing databases and other services.
Validation scope can use `workflow.validation.build_roots` and explicit
`exclusions` with reasons, as described in [the workflow guide](../notes/workflow.md);
those validation settings do not narrow stage 04 migration.

## Recipe development and CI references

| Reference | Use |
| --- | --- |
| [moderneinc/rewrite-recipe-starter](https://github.com/moderneinc/rewrite-recipe-starter) | Start custom Java/declarative recipes and their RewriteTest tests; package your recipe artifact. |
| [openrewrite/rewrite-testing-frameworks](https://github.com/openrewrite/rewrite-testing-frameworks) | Study production JUnit, AssertJ, and Mockito recipes and test patterns. |
| [Recipe starter CI workflow](https://github.com/moderneinc/rewrite-recipe-starter/blob/main/.github/workflows/ci.yml) | Upstream example of building/testing a recipe project in GitHub Actions. |
| [This tool's checks workflow](../.github/workflows/checks.yml) | Python workflow tests and opt-in real migration integration checks. |

The supplied `moderneinc/github-actions` URL returned GitHub HTTP 404 when checked;
use the recipe starter's actual workflow as a CI reference. Recipe libraries and
build templates are references here; the portfolio entries are application inputs.
To use a custom published recipe with this tool, add its fully qualified recipe
name and pinned artifact coordinate under `migration.openrewrite`.

For automation that publishes migration branches and draft PRs, this tool has
explicit `workflow.publishing` settings, including `targets: [src_repo]` and
`request.enabled: true`. See [publishing and request configuration](../notes/workflow.md).
The default configuration publishes to local repositories only.
