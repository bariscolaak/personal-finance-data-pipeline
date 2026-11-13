# Publishing this project

## Scope

Publish only the `financials_x01` project directory. Its parent directory is already a separate Git repository containing unrelated work. Do not initialize, stage, or push from the parent directory.

Suggested repository name: `personal-finance-data-pipeline`.

Suggested description: `Python and SQL pipeline for bank statements, cash flow, market prices, and multi-currency investment analytics with SQLite and Databricks.`

Start with a private repository. Before making it public for job applications, add a reproducible synthetic-data demo, the missing SQL definitions, and meaningful tests. Do not represent roadmap items as implemented features.

## Review locally

These commands are for the project directory in PowerShell. They have not been executed on your behalf.

```powershell
cd C:\Users\baris\OneDrive\Desktop\Baris\financials_x01
git init -b main
git rev-parse --show-toplevel
git status --short
```

The top-level path must end in `financials_x01`, not `Baris`.

Stage only the intended publication files:

```powershell
git add .gitignore .env.example README.md requirements.txt docs
git add import_pdf_to_db.py import_ceska_pdf_to_db.py import_json_to_db.py
git add import_cnb_fx_rates.py stock_prices.py sync_to_databricks.py web_app.py
git diff --cached --stat
git diff --cached
git ls-files
```

Review the full staged diff. It must not include `.env`, `.env` backups, database files, PDF statements, transaction JSON, real credentials, or personal financial screenshots. `.gitignore` prevents newly ignored files from being added normally; it does not remove files already committed.

The current `.gitignore` excludes the scratch scripts `test1.py` and `try_server.py`; they remain on disk.

After review:

```powershell
git commit -m "Document finance pipeline and add ingestion scripts"
```

## Create and push

On GitHub, create a private repository named `personal-finance-data-pipeline`. Leave automatic README, license, and gitignore initialization unchecked because the local project already contains its initial files. Choose a license deliberately before any later public release.

Copy the repository's HTTPS URL from GitHub, then run the following with your actual username:

```powershell
git remote add origin https://github.com/YOUR_USERNAME/personal-finance-data-pipeline.git
git push -u origin main
```

Complete Git's browser sign-in if prompted. Do not put an access token in the remote URL or source files.

## Portfolio presentation

Use the English README as the project landing page. Add topics such as `python`, `sql`, `etl`, `sqlite`, `databricks`, `data-engineering`, and `personal-finance`.

Show the engineering choices: source traceability, repeated-import handling, pagination validation, currency normalization, and trade versus portfolio cash flow. Add screenshots only after generating a synthetic dataset; hiding names alone does not anonymize personal balances and transactions.

For future changes, work on a focused branch, inspect the diff, run relevant checks, then commit and push. Small descriptive commits make the project's development easier to follow.
