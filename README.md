# Buy or Wait?

A local Streamlit demo with two separate experiences: a daily purchase affordability check using one locally saved profile, and a deterministic CSV benchmark evaluator for the “Buy or Wait?” task. Both forecast known cash flows over 90 days and protect a user-entered minimum balance. The CSV evaluator also compares supplied payment options and exports the required `output.csv` columns.

## Run locally

In VS Code, open this folder, then choose **Terminal → New Terminal**. In PowerShell, run:

```powershell
py -3 -m venv .venv-buy-or-wait
.\.venv-buy-or-wait\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
streamlit run app.py
```

If PowerShell blocks activation, skip the activation line and run:

```powershell
.\.venv-buy-or-wait\Scripts\python.exe -m pip install -r requirements.txt
.\.venv-buy-or-wait\Scripts\streamlit.exe run app.py
```

The app defaults to **public mode**, which fails closed until authentication and the cloud database are configured. To run the single-user local demo, copy the example settings once:

```powershell
if (!(Test-Path .streamlit/secrets.toml)) { Copy-Item .streamlit/secrets.example.toml .streamlit/secrets.toml }
```

The example file selects `mode = "local"`. `.streamlit/secrets.toml` is ignored by Git. To return to public mode, change `mode` to `"public"` after filling the Google and database settings below.

For daily use, open **My finances** and save your balance, reserve, and expected income/bills. Then use **Ask Buy or Wait** to estimate a planned purchase. Use **Benchmark CSV tool** separately to upload task CSVs; it accepts multiple CSVs at once and matches them by their original filenames. The app does not call a model provider or external banking service.

In local mode, one profile is saved in `local_finances.sqlite3` beside the app. It has no login or cloud sync. Public mode uses verified Google OIDC sign-in and stores each account's profile and cash flows in PostgreSQL, with row-level policies tied to the signed-in identity. The cloud connection uses TLS and a restricted app-only database role. Never use the database owner or a service-role credential as the app connection.

## Configure public multi-user mode

This project includes the code and deployment setup for **Streamlit Community Cloud + Google OIDC + Supabase PostgreSQL**. It is not live until those external accounts and credentials are configured. Credentials are not included in the repository. Configure a Supabase project, Google OAuth web client, and Streamlit Cloud app before public users can sign in.

1. In the Supabase SQL editor, run [`supabase/migrations/20261020000100_user_finances.sql`](supabase/migrations/20261020000100_user_finances.sql) as the database administrator. Replace the placeholder role password with a long random password first. The migration creates profile/cash-flow tables, a restricted app role, and row-level ownership policies. Use the `buy_wait_app` role for the app's PostgreSQL connection string.
2. In Google Auth Platform, create an OAuth client for a **Web application**. Add `http://localhost:8501/oauth2callback` for local public-mode checks and `https://YOUR-APP.streamlit.app/oauth2callback` after deployment. Publish the OAuth consent screen before opening sign-in to general Google accounts; Google's testing mode only allows configured test users.
3. Deploy this repository on Streamlit Community Cloud. In its **Advanced settings → Secrets**, paste settings based on `.streamlit/secrets.example.toml`, set `[app] mode = "public"`, replace the OIDC client values, use the deployed redirect URI, create a fresh long random `cookie_secret`, and add the Supabase connection URL for the `buy_wait_app` role. Keep secrets out of GitHub.
4. After deployment, test sign-in with two separate accounts. Each should see its own empty profile and should not see the other's balance or cash flows. Review the database policies and account deletion behavior before sharing it broadly.

Every personal database query is parameterized and runs in a transaction scoped to the verified identity. PostgreSQL row-level security is enabled and forced on both finance tables. The app never sends a database owner/service-role key to users. The benchmark CSV tool remains account-gated but does not save the uploaded CSVs to the cloud database.

## Dataset files

Core inputs: `requests.csv`, `financial_profiles.csv`, `financial_events.csv`, `exchange_rates.csv`, `request_payment_options.csv`, `messages.csv`, and `images.csv`.

Also provide `sample_requests.csv` and the blank `output.csv` template when available. Provide `media/images/<image_id>.png` for image-linked events with missing amounts. You can ZIP the image folder for upload.

## Deliverables and run status

The full participant run writes the required 250 rows to the root `output.csv`, in the blank template's request order. `code.zip` contains the app, local and cloud storage modules, benchmark forecasting engine, deployment files, README, and dependency list. Participant CSVs, image files, transcripts, benchmark outputs, and local databases are excluded from the public source package.

The two forecast-relevant blank amounts were read from their linked receipts: `event_6033` / `image_10.png` is INR 79,679.26 and `event_6859` / `image_11.png` is INR 3,650.00. The UI pre-fills these reviewed values and displays their linked images for confirmation. Sample requests and the output template are optional references in the app; only `requests.csv` rows are predicted.

## Current implementation notes

- Input column mapping supports common header variants and surfaces basic schema errors.
- Candidate plans are checked against the minimum balance on every forecast cash-flow date.
- Repeated settled cash flows with stable weekly-to-monthly cadence are projected, anchored by a supplied future payment when one exists.
- Future pending and scheduled debits/confirmed credits use settlement dates; pending credits, failed/canceled items, and unrealized valuations are excluded.
- Dated employer payroll amendments, explicit linked-event cancellations, and confirmed monthly salary cadence can update forecast inputs.
- Installment schedules preserve the supplied payment amount, count, dates, and total payable amount.
- The UI displays missing event amounts that affect a request forecast beside their linked image. Unreadable historical blanks are not treated as zero.
- The deterministic baseline has been compared with the supplied examples. It matches their general output structure and several plan choices, but it does not reproduce every reference value; review the output and audit before submission.

The purchase screen is an educational estimate, not personal financial advice. The supplied task dataset is a benchmark, not a general customer training set. Daily estimates use only the signed-in customer's saved profile and cash flows.

## Push the deliverables to GitHub

Create an **empty** GitHub repository first. In the VS Code terminal, from this project folder, initialize Git and stage only the Buy or Wait deliverables:

```powershell
git init -b main
git add .gitignore app.py affordability.py personal_mode.py cloud_storage.py README.md requirements.txt Dockerfile .dockerignore .streamlit/config.toml .streamlit/secrets.example.toml supabase/migrations/20261020000100_user_finances.sql
git status --short
git commit -m "Add Buy or Wait affordability app"
git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPOSITORY.git
git push -u origin main
```

Replace the remote URL with your repository URL. Review `git status --short` before committing. The staged list intentionally excludes benchmark outputs, participant CSVs, transcripts, local databases, and credentials. The local `dataset/` folder, virtual environments, and `.streamlit/secrets.toml` are ignored; do not add private financial data or credentials to GitHub.
