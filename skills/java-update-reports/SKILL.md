---
name: java-update-reports
description: Regenerate or interpret Markdown portfolio reports from staged Java update JSON artifacts. Use for repository, application, or application-group migration status and checklists; do not edit generated Markdown as source data.
---

# Java update reports

Treat `java-update.yml`, `repositories.yml`, and `.java-update/**/*.json` as source data. Markdown beneath `.java-update/runs/<run-id>/reports/` is disposable and must not be edited to change migration state.

From the repository root, regenerate every Markdown view with:

```bash
python3 scripts/render_reports.py --state .java-update --run-id latest
```

For a scoped report, use the portfolio CLI so repository associations are applied:

```bash
python3 portfolio.py report --repo REPO_ID
python3 portfolio.py report --application APPLICATION_ID
python3 portfolio.py report --application-group GROUP_ID
```

When summarizing results, report incomplete cohorts and unresolved alignment decisions explicitly. Preserve checklist state from JSON; never infer completion merely because a later-stage file exists.

Each six-stage run retains its own JSON and approvals. Include validation checks, exact validated commit, and per-target publishing receipts when reporting readiness. A migrated or analyzed engine result does not imply validation success. Manual review pauses return code 3; failed stages and partial publishing remain failures. Use `--run-id RUN_ID` to report a specific saved run.
