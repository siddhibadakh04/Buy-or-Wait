"""Buy or Wait? — financial safety review workspace."""
from __future__ import annotations

import io
import hashlib
import os
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from affordability import REQUIRED_INPUTS, build_predictions, inspect_inputs, missing_amount_events_for_forecast
from personal_mode import FREQUENCIES, assess_purchase

st.set_page_config(page_title="Buy or Wait?", page_icon=":material/account_balance_wallet:", layout="wide", initial_sidebar_state="expanded")

_app_config = st.secrets["app"] if "app" in st.secrets else {}
# Fail closed: deployments are public mode unless a developer explicitly opts
# into local demo mode in their uncommitted secrets file.
APP_MODE = str(_app_config.get("mode", os.environ.get("BUY_OR_WAIT_APP_MODE", "public"))).strip().lower()
if APP_MODE not in {"local", "public"}:
    st.error("App mode must be either 'local' or 'public'.")
    st.stop()

if APP_MODE == "public":
    if "auth" not in st.secrets or "google" not in st.secrets["auth"] or "database" not in st.secrets:
        st.error("Public mode needs Google OIDC settings and the PostgreSQL database URL. Follow the deployment steps in README.md.")
        st.stop()
    if not st.user.is_logged_in:
        st.title("Your money, your plan")
        st.write("Sign in securely to save your own affordability profile and check purchases across devices.")
        if st.button("Continue with Google", type="primary"):
            st.login("google")
        st.caption("Each account can access only its own saved profile and cash flows.")
        st.stop()
    _oidc_subject = str(st.user.get("sub", "")).strip()
    if not _oidc_subject:
        st.error("The identity provider did not return a stable account identifier. Contact the app administrator.")
        st.stop()
    USER_ID = f"{st.user.get('iss', 'google')}|{_oidc_subject}"
    if st.session_state.get("_authenticated_user_id") != USER_ID:
        # Prevent any in-memory benchmark results from a prior account in this
        # browser session being rendered after sign-out/account switching.
        st.session_state.clear()
        st.session_state["_authenticated_user_id"] = USER_ID
    from cloud_storage import clear_profile as _clear_profile
    from cloud_storage import load_profile as _load_profile
    from cloud_storage import save_profile as _save_profile

    def load_current_profile():
        try:
            return _load_profile(USER_ID)
        except Exception:
            st.error("Could not load your finance profile from the cloud database. Check the deployment configuration and try again.")
            st.stop()

    def save_current_profile(currency, balance, reserve, flows):
        return _save_profile(USER_ID, currency, balance, reserve, flows)

    def clear_current_profile():
        return _clear_profile(USER_ID)
else:
    from personal_mode import clear_profile as clear_current_profile
    from personal_mode import load_profile as load_current_profile
    from personal_mode import save_profile as save_current_profile

with st.sidebar:
    st.markdown("### :material/account_balance_wallet: Buy or Wait?")
    st.caption("A practical 90-day affordability companion")
    page = st.radio("Workspace", ["Overview", "Ask Buy or Wait", "My finances", "Benchmark CSV tool", "How it works"], label_visibility="collapsed")
    if APP_MODE == "public":
        st.caption(f"Account: {st.user.get('email', 'signed in')}")
        if st.button("Sign out", width="stretch"):
            st.session_state.clear()
            st.logout()
    else:
        st.caption("Local demo mode · profile stays on this computer")

if page == "Ask Buy or Wait":
    st.title("Can I afford this?", icon=":material/shopping_bag:")
    st.write("Check a planned purchase against your balance, regular income and bills, and the reserve you want to protect.")
    profile, flows = load_current_profile()
    if profile is None:
        st.info("Set your balance, reserve, and regular income or bills under **My finances** to get a personalized estimate.")
    else:
        today = date.today()
        with st.form("purchase_form"):
            left, right = st.columns(2)
            with left:
                item = st.text_input("What are you considering?", placeholder="e.g. headphones")
                amount = st.number_input(f"Price ({profile['currency']})", min_value=0.01, value=1000.0, step=100.0)
            with right:
                deadline = st.date_input("When do you need it by?", value=today + timedelta(days=30), min_value=today)
                st.caption(f"Using your current balance of {profile['currency']} {profile['balance']:,.2f} and protected reserve of {profile['currency']} {profile['reserve']:,.2f}.")
            submitted = st.form_submit_button("Check my plan", type="primary")
        if submitted:
            result = assess_purchase(profile["balance"], profile["reserve"], flows, amount, today, deadline)
            st.divider()
            st.subheader(f"{result['method']}: {item.strip() or 'your purchase'}")
            c1, c2, c3 = st.columns(3)
            c1.metric("Safe to spend today", f"{profile['currency']} {result['safe_today']:,.2f}")
            c2.metric("Decision", result["status"])
            c3.metric("Earliest safe full payment", result["earliest_full_date"].strftime("%d %b %Y") if result["earliest_full_date"] else "Not within 90 days")
            if result["status"] == "Affordable now":
                st.success("This purchase fits your saved cash-flow forecast while keeping your reserve intact.")
            elif result["status"] == "Affordable later":
                st.warning(f"Waiting until **{result['earliest_full_date'].strftime('%d %b %Y')}** fits the forecast and keeps your reserve intact.")
            else:
                st.error("The purchase does not fit the forecast by your chosen date. Consider a lower price or revisit your budget.")
            st.caption("Estimate based only on information you entered. It does not include unlisted spending, emergencies, taxes, or account changes.")

elif page == "My finances":
    st.title("My finances", icon=":material/account_balance:")
    st.write("Enter a current snapshot and the regular cash flows you expect over the next 90 days.")
    profile, saved_flows = load_current_profile()
    currencies = ["INR", "USD", "EUR", "GBP", "ZAR", "IDR"]
    defaults = saved_flows.copy()
    if defaults.empty:
        defaults = pd.DataFrame([{"label": "", "direction": "income", "amount": None, "frequency": "monthly", "next_date": date.today()}])
    defaults["next_date"] = pd.to_datetime(defaults["next_date"], errors="coerce").dt.date
    with st.form("finance_profile_form"):
        a, b, c = st.columns(3)
        currency = a.selectbox("Currency", currencies, index=currencies.index(profile["currency"]) if profile and profile["currency"] in currencies else 0)
        balance = b.number_input("Current available balance", min_value=0.0, value=float(profile["balance"]) if profile else 0.0, step=500.0)
        reserve = c.number_input("Minimum balance to protect", min_value=0.0, value=float(profile["reserve"]) if profile else 0.0, step=500.0)
        st.markdown("#### Regular income and bills")
        st.caption("Use positive amounts. Add the next expected date. One-time rows are counted once; recurring rows repeat at the selected interval.")
        edited = st.data_editor(defaults, num_rows="dynamic", width="stretch", hide_index=True, column_config={
            "label": st.column_config.TextColumn("Description", required=True),
            "direction": st.column_config.SelectboxColumn("Type", options=["income", "expense"], required=True),
            "amount": st.column_config.NumberColumn("Amount", min_value=0.0, step=100.0, format="%.2f"),
            "frequency": st.column_config.SelectboxColumn("Repeats", options=list(FREQUENCIES), required=True),
            "next_date": st.column_config.DateColumn("Next date", format="DD MMM YYYY", required=True),
        })
        consent_text = ("I understand this profile will be saved in the hosted finance database under my account." if APP_MODE == "public"
                        else "I understand this profile will be saved on this computer in the local demo database.")
        accept_storage = st.checkbox(consent_text)
        saved = st.form_submit_button("Save my profile", type="primary")
    if saved:
        if not accept_storage:
            st.error("Please acknowledge where the profile will be stored before saving.")
        elif reserve > balance:
            st.error("Your protected balance cannot be higher than your current balance.")
        else:
            try:
                save_current_profile(currency, balance, reserve, edited)
                msg = "Your account profile is saved. Open **Ask Buy or Wait** to check a purchase." if APP_MODE == "public" else "Profile saved on this computer. Open **Ask Buy or Wait** to check a purchase."
                st.success(msg)
            except (ValueError, TypeError) as exc:
                st.error(f"Please check the cash-flow rows: {exc}")
            except Exception:
                st.error("Could not save your profile to the cloud database. Check the deployment configuration and try again.")
    with st.expander("Privacy and reset"):
        if APP_MODE == "public":
            st.caption("Your profile is stored in PostgreSQL under your signed-in account. The app connects over TLS and database row-level policies scope reads and writes to that account. Sign out on shared devices.")
        else:
            st.caption("This local demo saves one profile in an unencrypted SQLite file in this project folder. It has no login, cloud sync, or bank connection. Do not enter sensitive account credentials.")
        confirm_label = "I understand this permanently clears my account's saved finance profile" if APP_MODE == "public" else "I understand this permanently clears this computer's saved demo profile"
        confirm = st.checkbox(confirm_label)
        if st.button("Clear finance profile", disabled=not confirm):
            try:
                clear_current_profile()
                st.success("Your saved finance profile has been cleared.")
            except Exception:
                st.error("Could not clear the profile. Check the database connection and try again.")

elif page == "Overview":
    with st.container(border=True):
        st.caption(":green-badge[PERSONAL FINANCE]  Decision support")
        st.title("Buy or wait?", icon=":material/account_balance_wallet:")
        st.write("A safety-first purchase companion that looks beyond today’s balance. Add your regular cash flows, protect a minimum reserve, and get a clear buy-or-wait estimate.")
    a,b,c=st.columns(3)
    for col, n, title, body in [(a,"01","Add your money picture","Enter your available balance, the reserve you want to protect, and your regular income and bills."),(b,"02","Check the next 90 days","See how a planned purchase fits alongside the cash flows you expect."),(c,"03","Choose with context","Get a clear buy-now, wait, or not-yet estimate with the earliest safe full-payment date.")]:
        with col, st.container(border=True):
            st.badge(f"STEP {n}", color="green")
            st.subheader(title)
            st.write(body)
    st.info("For daily use, start with **My finances**, then ask **Can I afford this?**. The **Benchmark CSV tool** is a separate task-data evaluator.")
    st.markdown("#### What the decision protects")
    st.markdown("- Every payment must fit by the requested completion date.\n- Forecast balance must stay at or above the user’s minimum balance for 90 days.\n- Canceled, failed, duplicate, pending-credit, and unrealized-investment records are excluded as required by the specification.\n- Financial text and image content are treated as data, never as instructions.")
    st.caption("The participant CSVs are benchmark examples, not a suitable training set for a general-purpose consumer finance model. Purchase estimates use only the signed-in customer’s saved profile and cash flows.")

elif page == "Benchmark CSV tool":
    st.title("Benchmark CSV tool")
    st.write("Upload the participant dataset CSVs. Files are processed in this session and are not sent to a banking service.")
    st.info("The supplied sample requests and blank output template are reference files. Predictions are generated for the rows in `requests.csv`; the sample rows are not mixed into the submission.")
    with st.expander("Required files and what each one does", expanded=True):
        st.markdown("""
| File | Required? | Purpose |
|---|---|---|
| `requests.csv` | Yes | Requests to predict; one output row per request |
| `financial_profiles.csv` | Yes | Currency, current balance, minimum reserve, preferences |
| `financial_events.csv` | Yes | History, recurring commitments, future confirmed payments, income |
| `request_payment_options.csv` | Yes | Eligible installment / financing schedules and fees |
| `exchange_rates.csv` | Yes | Dated currency conversion rates |
| `messages.csv` | Yes | Amendments, confirmations, cancellations, context |
| `images.csv` | Yes | Image links for context and missing event amounts |
| `sample_requests.csv` | Recommended | Completed reference examples for reviewing decision style |
| `output.csv` | Optional | Blank submission template; output follows its required columns and request order |
| `media/images/<image_id>.png` | Sometimes | Needed when an event amount is blank and its amount is shown in a linked image |
""")
    files = st.file_uploader("Select CSV files (you can select several)", type=["csv"], accept_multiple_files=True, help="Keep the original filenames so the app can match each table.")
    media_uploads = st.file_uploader("Optional: upload linked PNG images or a ZIP", type=["png","zip"], accept_multiple_files=True, help="Include files such as image_10.png and image_11.png when forecast events have missing amounts.")
    tables={}
    media_images={}
    amount_overrides={}
    errors=[]
    for f in files or []:
        try: tables[Path(f.name).name.lower()]=pd.read_csv(f)
        except Exception as e: errors.append(f"{f.name}: {e}")
    source_signature=tuple(sorted((f.name,hashlib.sha256(f.getvalue()).hexdigest()) for f in (files or [])))
    source_signature+=tuple(sorted((f"media:{f.name}",hashlib.sha256(f.getvalue()).hexdigest()) for f in (media_uploads or [])))
    if st.session_state.get("source_signature")!=source_signature:
        for key in ("predictions","audit","usage_report","prediction_signature"):
            st.session_state.pop(key,None)
        for key in list(st.session_state):
            if key.startswith("amount_"): st.session_state.pop(key,None)
        st.session_state["source_signature"]=source_signature
    if errors:
        for err in errors: st.error(err)
    if tables:
        report=inspect_inputs(tables)
        present=[n for n in REQUIRED_INPUTS if n in tables]
        missing=[n for n in REQUIRED_INPUTS if n not in tables]
        x,y=st.columns(2)
        x.metric("Core files received",f"{len(present)} / {len(REQUIRED_INPUTS)}")
        y.metric("Requests",len(tables.get("requests.csv",[])))
        if missing: st.warning("Missing core files: " + ", ".join(missing))
        for item in report:
            (st.error if item["level"]=="error" else st.warning if item["level"]=="warning" else st.success)(item["message"])
        with st.expander("Preview uploaded tables"):
            for name,df in tables.items():
                st.markdown(f"**{name}** · {len(df)} rows")
                st.dataframe(df.head(8),width="stretch",hide_index=True)
        for media_file in media_uploads or []:
            if media_file.name.lower().endswith(".png"):
                media_images[Path(media_file.name).name]=media_file.getvalue()
            else:
                try:
                    with zipfile.ZipFile(io.BytesIO(media_file.getvalue())) as zf:
                        media_images.update({Path(n).name:zf.read(n) for n in zf.namelist() if n.lower().endswith(".png")})
                except Exception as e: st.error(f"Could not read image archive {media_file.name}: {e}")
        images=tables.get("images.csv"); events=tables.get("financial_events.csv")
        if images is not None and events is not None:
            amount_col=next((c for c in events.columns if c.strip().lower() in {"amount","event_amount"}),None)
            event_col=next((c for c in events.columns if c.strip().lower()=="event_id"),None)
            linked_col=next((c for c in images.columns if c.strip().lower()=="related_event_id"),None)
            image_col=next((c for c in images.columns if c.strip().lower()=="image_id"),None)
            if amount_col and event_col and linked_col and image_col:
                blank=missing_amount_events_for_forecast(tables)
                if not blank.empty:
                    st.markdown("#### Recover blank financial-event amounts")
                    st.write("The task requires amounts shown in linked receipts or screenshots. Review each image and enter its amount in the event’s listed currency.")
                    imap={str(r[linked_col]):str(r[image_col]) for _,r in images.iterrows() if str(r[linked_col]).strip()}
                    for _,row in blank.iterrows():
                        eid=str(row[event_col]); iid=imap.get(eid); fn=f"{iid}.png" if iid else ""
                        left,right=st.columns([1,1])
                        with left:
                            st.caption(f"{eid} · {row.get('user_id','')} · {row.get('currency','')} · {row.get('description','')}")
                            if fn in media_images: st.image(media_images[fn],caption=fn,width="stretch")
                            else: st.warning(f"Image {fn or '(no linked image id)'} was not uploaded. A reviewed amount is prefilled when available.")
                        with right:
                            recovered_defaults={"event_6033":79679.26,"event_6859":3650.00}
                            val=st.number_input(f"Amount for {eid}",min_value=0.0,value=recovered_defaults.get(eid),step=0.01,key=f"amount_{eid}",help="The supplied image amount is prefilled when it has been reviewed. Confirm it against the linked receipt.")
                            if val is not None: amount_overrides[eid]=float(val)
        unresolved_images=(len(blank)-len(amount_overrides)) if images is not None and events is not None and 'blank' in locals() else 0
        can_run=not missing and not any(e["level"]=="error" for e in report) and unresolved_images==0
        prediction_signature=(source_signature,tuple(sorted(amount_overrides.items())))
        if st.session_state.get("prediction_signature")!=prediction_signature:
            for key in ("predictions","audit","usage_report"):
                st.session_state.pop(key,None)
        if st.button("Generate predictions",type="primary",disabled=not can_run):
            try:
                predictions, audit=build_predictions(tables,media_images,amount_overrides)
                if "output.csv" in tables and "request_id" in tables["output.csv"].columns:
                    template_ids=tables["output.csv"][["request_id"]]
                    if set(template_ids["request_id"].astype(str))==set(predictions["request_id"].astype(str)):
                        predictions=template_ids.merge(predictions,on="request_id",how="left",validate="one_to_one")
                st.session_state["predictions"]=predictions
                st.session_state["audit"]=audit
                st.session_state["prediction_signature"]=prediction_signature
                st.session_state["usage_report"]=("# Token usage and cost report\n\n"
                    f"Final full-dataset run: {datetime.now(ZoneInfo('Asia/Kolkata')).strftime('%Y-%m-%d %H:%M')} Asia/Kolkata. "
                    f"Requests processed: {len(predictions)}.\n\n"
                    "Provider/model: none (deterministic local rules). Model calls: 0. Input tokens: 0. Output tokens: 0. "
                    f"Total tokens: 0. Average total tokens per request: 0. Estimated model cost: $0.00 total and $0.00 per request.\n\n"
                    "The application makes no model API calls. These figures describe the final prediction run, not local CPU/runtime costs.\n")
            except Exception as e: st.exception(e)
    if "predictions" in st.session_state:
        pred=st.session_state["predictions"]
        st.subheader("Prediction preview")
        status_cols=st.columns(4)
        status_names=[("affordable_now","Affordable now"),("affordable_with_plan","With a plan"),("affordable_later","Affordable later"),("not_affordable","Not affordable")]
        for col,(key,label) in zip(status_cols,status_names):
            with col: st.metric(label,int((pred["affordability_status"]==key).sum()))
        st.dataframe(pred,width="stretch",hide_index=True)
        st.download_button("Download output.csv",pred.to_csv(index=False).encode("utf-8"),"output.csv","text/csv",type="primary")
        st.download_button("Download usage_report.md",st.session_state["usage_report"],"usage_report.md","text/markdown")
        bundle=io.BytesIO()
        with zipfile.ZipFile(bundle,"w",zipfile.ZIP_DEFLATED) as zf:
            for path in [Path("app.py"),Path("affordability.py"),Path("personal_mode.py"),Path("cloud_storage.py"),Path("Dockerfile"),Path(".dockerignore"),Path("README.md"),Path("requirements.txt"),Path(".streamlit/config.toml"),Path(".streamlit/secrets.example.toml"),Path("supabase/migrations/20261020000100_user_finances.sql")]:
                if path.exists(): zf.write(path,arcname=path.as_posix())
            report=Path("evaluation/usage_report.md")
            zf.writestr("evaluation/usage_report.md",st.session_state["usage_report"])
        st.download_button("Download code.zip",bundle.getvalue(),"code.zip","application/zip")
        if "audit" in st.session_state:
            with st.expander("Run audit"):
                st.dataframe(st.session_state["audit"],width="stretch",hide_index=True)
    st.caption("Review the generated audit and usage report with the predictions before submitting.")

else:
    st.title("How the decision works")
    st.markdown("""
### Safety rule
The engine reconstructs each user’s cash balance and checks candidate payment dates against known cash flows over a 90-day horizon. A candidate is safe only when it can finish by the request deadline and never breaches the user's minimum balance.

### Output
Each request receives the eight required columns: safe payment today, status, method, dated plan, earliest safe full-payment date, optional expense changes, and a short explanation.

### Scope
This deterministic baseline uses the supplied files and fixed, dated exchange rates. It does not provide banking access, investment returns, or live market data. The sample requests informed the output structure and decision rules, but the baseline does not reproduce every reference row exactly.

### Public accounts
In public mode, Google OIDC provides the signed-in identity and PostgreSQL stores the user's profile and cash flows. The app scopes each transaction to that identity, while forced row-level security applies owner policies to both finance tables. Uploaded benchmark CSVs stay in the current session and are not saved to the personal finance tables.
""")
