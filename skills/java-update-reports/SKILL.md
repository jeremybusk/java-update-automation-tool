---
name: java-update-reports
description: Regenerate or interpret Markdown portfolio reports from staged Java update JSON artifacts. Use for repository, application, or application-group migration status and checklists; do not edit generated Markdown as source data.
---

# Java update reports

Treat `java-update.yml`, `repositories.yml`, and `.java-update/**/*.json` as source data. Markdown beneath `.java-update/reports/` is disposable and must not be edited to change migration state.

From the repository root, regenerate every Markdown view with:

```bash
python3 scripts/render_reports.py --state .java-update
```

For a scoped report, use the portfolio CLI so repository associations are applied:

```bash
python3 portfolio.py report --repo REPO_ID
python3 portfolio.py report --application APPLICATION_ID
python3 portfolio.py report --application-group GROUP_ID
```

When summarizing results, report incomplete cohorts and unresolved alignment decisions explicitly. Preserve checklist state from JSON; never infer completion merely because a later-stage file exists.
