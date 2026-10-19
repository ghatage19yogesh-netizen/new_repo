"""
Product Exclusion Tool — business-friendly front end for the Dynamic Fulfillment
SKU exclusion tables.

Runs inside the portal (streamlit_app.py). Session keys are prefixed df_ so they never clash with
the Allocation Planner.

Practice mode (dummy data) is the default

Run against BigQuery:
    streamlit run streamlit_app.py -- --backend bigquery --env dev
    streamlit run streamlit_app.py -- --backend bigquery --env prod
"""
import argparse
import inspect
import io
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

_NEW_WIDTH = "width" in inspect.signature(st.button).parameters
WIDE = {"width": "stretch"} if _NEW_WIDTH else {"use_container_width": True}

# --------------------------------------------------------------------------- #
# Business-facing configuration — edit descriptions to match your teams' words
# --------------------------------------------------------------------------- #
PROCESS_CONFIG = {
    "cs_margin": {
        "name": "CS Margin",
        "table_name": "cs_margin_excluded_skus",
        "description": "Products left out of the CS margin calculation.",
    },
    "ctf": {
        "name": "Sellout Risk",
        "table_name": "ctf_excluded_skus",
        "description": "Products left out of sell-out risk checks.",
    },
    "replen": {
        "name": "Replenishment",
        "table_name": "rplnsh_excluded_skus",
        "description": "Products left out of automatic replenishment.",
    },
    "vdf_margin": {
        "name": "VDF Margin",
        "table_name": "vdf_margin_excluded_skus",
        "description": "Products left out of the VDF margin calculation.",
    },
}

DATASETS = {
    "bigquery": {"prod": "mtech-daas-appdata.ewphoc", "dev": "mcy-eda-anlytcs-sbox.dynamic_fulfillment_dev"},
    "demo": {"prod": "demo-project.df_prod", "dev": "demo-project.df_dev"},
}

DEMO_SEED = {
    "cs_margin": [100000000001, 100000000002, 100000000003, 100000000004, 100000000005],
    "ctf": [100000000001, 200000000002, 200000000003],
    "replen": [300000000001, 300000000002, 100000000003, 300000000004],
    "vdf_margin": [400000000001, 100000000002],
}

ENV_LABEL = {"dev": "Test", "prod": "Live"}

# Time zones offered when scheduling a change (step 3 → "Schedule for a US time").
# Daylight saving (EST/EDT etc.) is handled automatically.
US_TIMEZONES = {
    "Eastern (ET)": "America/New_York",
    "Central (CT)": "America/Chicago",
    "Mountain (MT)": "America/Denver",
    "Arizona (MST)": "America/Phoenix",
    "Pacific (PT)": "America/Los_Angeles",
    "Alaska (AKT)": "America/Anchorage",
    "Hawaii (HT)": "Pacific/Honolulu",
}
IST = ZoneInfo("Asia/Kolkata")
SCHEDULE_TABLE = "df_scheduled_changes"  # BigQuery table (in the same dataset) that holds scheduled changes
SRC_PASTE, SRC_UPLOAD, SRC_SCHEDULE = "Type or paste numbers", "Upload a file", "Schedule for a US time"


def fmt_us(dt_utc, tzname):
    loc = dt_utc.astimezone(ZoneInfo(tzname))
    return loc.strftime("%a %d %b %Y, %I:%M %p ") + loc.tzname()


def fmt_ist(dt_utc):
    return dt_utc.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", "--run-env", "--run_env", "-e", dest="run_env", default=None)
    ap.add_argument("--backend", choices=["demo", "bigquery"], default="demo")
    a, _ = ap.parse_known_args(sys.argv[1:])
    return a


CLI = _cli()
BACKEND = CLI.backend
CLI_ENV = (CLI.run_env or "").strip().lower() or None
DEFAULT_ENV = CLI_ENV or os.getenv("RUN_ENV", "dev").strip().lower()
if DEFAULT_ENV not in ("dev", "prod"):
    DEFAULT_ENV = "dev"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def split_entries(text: str):
    """Returns (unique valid product numbers in order, duplicates count, invalid entries)."""
    tokens = [t for t in re.split(r"[\s,;]+", text.strip()) if t]
    valid, invalid = [], []
    for t in tokens:
        t2 = t[:-2] if t.endswith(".0") else t  # Excel often turns 123 into 123.0
        (valid if t2.isdigit() else invalid).append(t2 if t2.isdigit() else t)
    nums = [int(v) for v in valid]
    unique = list(dict.fromkeys(nums))
    return unique, len(nums) - len(unique), list(dict.fromkeys(invalid))


def read_upload(file) -> str:
    name = file.name.lower()
    if name.endswith((".xlsx", ".xls")):
        df = pd.read_excel(file, header=None, dtype=str)
    else:
        df = pd.read_csv(io.StringIO(file.getvalue().decode("utf-8", "ignore")), header=None, dtype=str)
    col = df.iloc[:, 0].dropna().astype(str)
    return "\n".join(col)


# --------------------------------------------------------------------------- #
# Data backends
# --------------------------------------------------------------------------- #
class DemoBackend:
    def __init__(self, env):
        self.env = env
        db = st.session_state.setdefault("df_demo_db", {})
        if env not in db:
            db[env] = {k: set(v) for k, v in DEMO_SEED.items()}
        self.tables = db[env]
        st.session_state.setdefault("df_demo_job_n", 1)

    def existing(self, skus, pid, full_table):
        return {s for s in skus if s in self.tables[pid]}

    def apply(self, action, skus, pid, full_table):
        t = self.tables[pid]
        if action == "add":
            new = [s for s in skus if s not in t]
            t.update(new)
            n = len(new)
        else:
            n = sum(1 for s in skus if s in t)
            t.difference_update(skus)
        ref = f"PRACTICE-{st.session_state.df_demo_job_n:04d}"
        st.session_state.df_demo_job_n += 1
        return ref, n

    # ---- scheduled changes (kept for this browser session in practice mode) ----
    def _jobs(self):
        return st.session_state.setdefault("df_demo_sched", {}).setdefault(self.env, [])

    def schedule(self, job):
        self._jobs().append(dict(job))

    def list_jobs(self):
        return [dict(j) for j in self._jobs()]

    def _find(self, job_id):
        return next((j for j in self._jobs() if j["job_id"] == job_id), None)

    def claim(self, job_id):
        j = self._find(job_id)
        if j and j["status"] == "Scheduled":
            j["status"] = "Running"
            return True
        return False

    def finish(self, job_id, status, count, ref):
        j = self._find(job_id)
        if j:
            j.update(status=status, applied_at=datetime.now(timezone.utc), applied_count=count, result_ref=ref)

    def cancel(self, job_id):
        j = self._find(job_id)
        if j and j["status"] == "Scheduled":
            j["status"] = "Cancelled"
            return True
        return False


@st.cache_resource
def _bq_client():
    from google.cloud import bigquery
    return bigquery.Client()


class BigQueryBackend:
    def __init__(self, env):
        from google.cloud import bigquery
        self.bq = bigquery
        self.client = _bq_client()
        self.sched_table = f"{DATASETS['bigquery'][env]}.{SCHEDULE_TABLE}"
        self._sched_ready = False

    # ---- scheduled changes (stored in BigQuery so every user and session sees them) ----
    def _q(self, sql, params=()):
        job = self.client.query(sql, job_config=self.bq.QueryJobConfig(query_parameters=list(params)))
        rows = list(job.result())
        return job, rows

    def _ensure_sched(self):
        if not self._sched_ready:
            self._q(f"""CREATE TABLE IF NOT EXISTS `{self.sched_table}` (
                job_id STRING, action STRING, process_id STRING, skus ARRAY<NUMERIC>,
                run_at TIMESTAMP, tz STRING, status STRING, created_by STRING, created_at TIMESTAMP,
                applied_at TIMESTAMP, applied_count INT64, result_ref STRING)""")
            self._sched_ready = True

    def schedule(self, job):
        self._ensure_sched()
        P, A = self.bq.ScalarQueryParameter, self.bq.ArrayQueryParameter
        self._q(f"""INSERT INTO `{self.sched_table}`
                    (job_id, action, process_id, skus, run_at, tz, status, created_by, created_at)
                    VALUES (@id, @action, @pid, @skus, @run_at, @tz, 'Scheduled', @by, CURRENT_TIMESTAMP())""",
                [P("id", "STRING", job["job_id"]), P("action", "STRING", job["action"]),
                 P("pid", "STRING", job["process_id"]), A("skus", "NUMERIC", [Decimal(s) for s in job["skus"]]),
                 P("run_at", "TIMESTAMP", job["run_at"]), P("tz", "STRING", job["tz"]),
                 P("by", "STRING", job.get("created_by", ""))])

    def list_jobs(self):
        self._ensure_sched()
        _, rows = self._q(f"SELECT * FROM `{self.sched_table}` ORDER BY run_at DESC LIMIT 500")
        out = []
        for r in rows:
            d = dict(r.items())
            d["skus"] = [int(s) for s in (d.get("skus") or [])]
            out.append(d)
        return out

    def _set_status(self, job_id, frm, to):
        P = self.bq.ScalarQueryParameter
        job, _ = self._q(f"UPDATE `{self.sched_table}` SET status = @to WHERE job_id = @id AND status = @frm",
                         [P("id", "STRING", job_id), P("frm", "STRING", frm), P("to", "STRING", to)])
        return (job.num_dml_affected_rows or 0) == 1

    def claim(self, job_id):  # only one session can claim a due job, so it never runs twice
        return self._set_status(job_id, "Scheduled", "Running")

    def cancel(self, job_id):
        return self._set_status(job_id, "Scheduled", "Cancelled")

    def finish(self, job_id, status, count, ref):
        P = self.bq.ScalarQueryParameter
        self._q(f"""UPDATE `{self.sched_table}` SET status = @st, applied_at = CURRENT_TIMESTAMP(),
                    applied_count = @n, result_ref = @ref WHERE job_id = @id""",
                [P("id", "STRING", job_id), P("st", "STRING", status), P("n", "INT64", count),
                 P("ref", "STRING", str(ref)[:1000])])

    def _cfg(self, skus):
        return self.bq.QueryJobConfig(query_parameters=[
            self.bq.ArrayQueryParameter("skus", "NUMERIC", [Decimal(s) for s in skus])])

    def existing(self, skus, pid, full_table):
        if not skus:
            return set()
        q = f"SELECT sku_upc_nbr FROM `{full_table}` WHERE sku_upc_nbr IN UNNEST(@skus)"
        return {int(r["sku_upc_nbr"]) for r in self.client.query(q, job_config=self._cfg(skus)).result()
                if r["sku_upc_nbr"] is not None}

    def apply(self, action, skus, pid, full_table):
        if action == "add":
            q = (f"INSERT INTO `{full_table}` (dept_nbr, sku_upc_nbr) "
                 f"SELECT 0, sku FROM UNNEST(@skus) AS sku")
        else:
            q = f"DELETE FROM `{full_table}` WHERE sku_upc_nbr IN UNNEST(@skus)"
        job = self.client.query(q, job_config=self._cfg(skus))
        job.result()
        n = job.num_dml_affected_rows if job.num_dml_affected_rows is not None else len(skus)
        return job.job_id, n


# --------------------------------------------------------------------------- #
# Page setup & styling
# --------------------------------------------------------------------------- #
st.markdown("""
<style>
  .block-container { padding-top: 2rem; max-width: 860px; }
  .step-title { font-size: 1.15rem; font-weight: 600; margin: 1.4rem 0 .3rem; }
  .step-num { display:inline-block; width:1.7rem; height:1.7rem; line-height:1.7rem; text-align:center;
              border-radius:50%; background:#1f4e79; color:#fff; font-size:.9rem; margin-right:.5rem; }
  .summary-card { border-radius: 12px; padding: 1rem 1.2rem; margin: .5rem 0; font-size: 1.02rem; }
  .card-go   { background:#e7f5ec; border-left: 6px solid #1f7a4d; }
  .card-skip { background:#eef2f7; border-left: 6px solid #6b7a90; }
  .card-bad  { background:#fff4e0; border-left: 6px solid #b26a00; }
  .big { font-size: 1.6rem; font-weight: 700; margin-right: .4rem; }
  .env-live { background:#b42318; color:#fff; padding:.15rem .6rem; border-radius:999px; font-weight:600; font-size:.8rem; }
  .env-test { background:#e8eefb; color:#2b59c3; padding:.15rem .6rem; border-radius:999px; font-weight:600; font-size:.8rem; }
</style>
""", unsafe_allow_html=True)

for k, v in {"df_log": [], "df_review": None, "df_done": None}.items():
    st.session_state.setdefault(k, v)


def step(n, text):
    st.markdown(f'<div class="step-title"><span class="step-num">{n}</span>{text}</div>', unsafe_allow_html=True)


def plural(n, word="product"):
    return f"{n} {word}{'' if n == 1 else 's'}"


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    if "PORTAL" in globals():
        PORTAL["sidebar"]()
    st.markdown("### Settings")
    if BACKEND == "demo":
        st.success("**Practice mode**\n\nNothing you do here changes real data. Feel free to try things out.")
        env = st.radio("Which system?", ["dev", "prod"], format_func=lambda e: ENV_LABEL[e],
                       index=1 if DEFAULT_ENV == "prod" else 0, key="df_env",
                       help="Test = safe sandbox. Live = affects real fulfillment.")
    else:
        env = DEFAULT_ENV
        st.write(f"System: **{ENV_LABEL[env]}**")
    dataset = DATASETS[BACKEND][env]

    st.markdown("### How it works")
    st.markdown(
        "1. Choose whether to **exclude** products or **bring them back**.\n"
        "2. Pick the **process** it applies to.\n"
        "3. Paste product numbers, upload a file, or **schedule** the change for a US date & time.\n"
        "4. **Review** the plain-English summary.\n"
        "5. **Confirm** — the change applies straight away, or at the time you scheduled."
    )
    st.caption("Questions? Contact the Dynamic Fulfillment team.")

backend = DemoBackend(env) if BACKEND == "demo" else BigQueryBackend(env)
full_table = lambda pid: f"{dataset}.{PROCESS_CONFIG[pid]['table_name']}"
CURRENT_USER = PORTAL["user"]["name"] if "PORTAL" in globals() else ""


# --------------------------------------------------------------------------- #
# Scheduled changes — applied once their US date & time has passed
# --------------------------------------------------------------------------- #
def run_due_jobs():
    """Apply every scheduled change whose time has come. Returns how many ran."""
    now = datetime.now(timezone.utc)
    ran = 0
    for j in backend.list_jobs():
        if j["status"] != "Scheduled" or j["run_at"] > now:
            continue
        if not backend.claim(j["job_id"]):
            continue  # another session picked it up
        pid, is_add = j["process_id"], j["action"] == "add"
        try:
            existing = backend.existing(j["skus"], pid, full_table(pid))
            go = [s for s in j["skus"] if (s not in existing) == is_add]
            ref, n = backend.apply(j["action"], go, pid, full_table(pid)) if go else ("nothing to change", 0)
            backend.finish(j["job_id"], "Done", n, ref)
            st.session_state.df_log.insert(0, {
                "When": datetime.now().strftime("%d %b %Y, %H:%M"), "System": ENV_LABEL[env],
                "Change": f"{'Excluded' if is_add else 'Brought back'} (scheduled {fmt_us(j['run_at'], j['tz'])})",
                "Process": PROCESS_CONFIG[pid]["name"], "Products": n, "Reference": ref,
                "Product numbers": ", ".join(map(str, go)),
            })
        except Exception as e:
            backend.finish(j["job_id"], "Failed", 0, f"Error: {e}")
        ran += 1
    return ran


@st.fragment(run_every=60)
def scheduler_tick():
    """Checks for due scheduled changes every minute while the app is open."""
    try:
        if run_due_jobs():
            st.rerun()
    except Exception:
        pass  # never block the page if the schedule can't be read


with st.sidebar:
    scheduler_tick()

# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
badge = '<span class="env-live">LIVE SYSTEM</span>' if env == "prod" else '<span class="env-test">TEST SYSTEM</span>'
st.markdown(f"## 🛒 Product Exclusion Tool &nbsp;{badge}", unsafe_allow_html=True)
st.write("Stop specific products from being picked up by a fulfillment process, or bring them back.")
if env == "prod":
    st.error("You're working on the **Live** system. Changes take effect immediately.", icon="⚠️")

tab_change, tab_check, tab_sched, tab_log = st.tabs(
    ["✏️ Make a change", "🔍 Check a product", "⏰ Scheduled", "📋 Activity log"])

# --------------------------------------------------------------------------- #
# TAB 1 — Make a change
# --------------------------------------------------------------------------- #
def change_tab():
    done = st.session_state.df_done
    if done:
        if not done.get("shown"):
            st.balloons()
        done["shown"] = True
        st.success(f"### ✅ Done\n{done['message']}")
        st.caption(f"Reference: {done['ref']}  ·  {done['time']}")
        if st.button("Make another change", type="primary"):
            st.session_state.df_done = None
            st.session_state.df_review = None
            for k in ("df_entries", "df_ack", "df_sched_up", "df_sched_date", "df_sched_time"):
                st.session_state.pop(k, None)
            st.rerun()
        return

    step(1, "What would you like to do?")
    ACTIONS = {
        "Exclude products": "add",
        "Bring products back": "remove",
    }
    action_label = st.radio(
        "action", list(ACTIONS), key="df_action", label_visibility="collapsed", horizontal=True,
        captions=["The process will ignore these products.", "The process will include these products again."],
    )
    action = ACTIONS[action_label]

    step(2, "Which process does this apply to?")
    pids = list(PROCESS_CONFIG)
    pid = st.radio(
        "process", pids, index=None, key="df_process", label_visibility="collapsed",
        format_func=lambda p: PROCESS_CONFIG[p]["name"],
        captions=[PROCESS_CONFIG[p]["description"] for p in pids],
    )

    step(3, "Which products?")
    src = st.radio("source", [SRC_PASTE, SRC_UPLOAD, SRC_SCHEDULE], horizontal=True,
                   key="df_src", label_visibility="collapsed")
    run_at = sched_tz = None
    if src == SRC_SCHEDULE:
        st.caption(f"These products will be **{'excluded' if action == 'add' else 'brought back'}** "
                   "automatically at the US date & time you pick.")
        text = st.text_area("Product numbers", key="df_entries", height=150, label_visibility="collapsed",
                            placeholder="Paste product numbers (UPCs) here — one per line, or separated by commas")
        up = st.file_uploader("…or upload Excel / CSV (product numbers in the first column)",
                              type=["xlsx", "xls", "csv"], key="df_sched_up")
        if up is not None:
            try:
                text = "\n".join(x for x in (text, read_upload(up)) if x.strip())
            except Exception as e:
                st.error(f"Couldn't read that file. Please check it's a normal Excel or CSV file. ({e})")

        c1, c2, c3 = st.columns([1.3, 1, 1])
        tz_label = c1.selectbox("US time zone", list(US_TIMEZONES), key="df_sched_tz")
        sched_tz = US_TIMEZONES[tz_label]
        now_us = datetime.now(ZoneInfo(sched_tz))
        nxt = (now_us + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        st.session_state.setdefault("df_sched_date", nxt.date())
        st.session_state.setdefault("df_sched_time", nxt.time())
        d = c2.date_input("Date", key="df_sched_date", min_value=now_us.date(), format="MM/DD/YYYY")
        t = c3.time_input("Time", key="df_sched_time", step=timedelta(minutes=15))
        run_at = datetime.combine(d, t).replace(tzinfo=ZoneInfo(sched_tz)).astimezone(timezone.utc)
        if run_at <= datetime.now(timezone.utc):
            st.warning("That time has already passed in the US time zone you picked — please choose a later time.")
            run_at = None
        else:
            st.markdown(f"🕒 Runs on **{fmt_us(run_at, sched_tz)}** &nbsp;·&nbsp; "
                        f"that's {fmt_ist(run_at)} / {run_at.strftime('%H:%M')} UTC")
    elif src == SRC_UPLOAD:
        up = st.file_uploader("Excel or CSV — product numbers in the first column", type=["xlsx", "xls", "csv"])
        text = ""
        if up is not None:
            try:
                text = read_upload(up)
            except Exception as e:
                st.error(f"Couldn't read that file. Please check it's a normal Excel or CSV file. ({e})")
    else:
        text = st.text_area("Product numbers", key="df_entries", height=150, label_visibility="collapsed",
                            placeholder="Paste product numbers (UPCs) here — one per line, or separated by commas")

    skus, dups, invalid = split_entries(text)
    if text.strip():
        bits = [f"**{plural(len(skus))}** found"]
        if dups:
            bits.append(f"{dups} repeated (counted once)")
        if invalid:
            bits.append(f"{len(invalid)} not recognised")
        st.caption(" · ".join(bits))

    scheduled = src == SRC_SCHEDULE
    signature = (env, action, pid, text, scheduled, run_at, sched_tz)
    can_review = bool(pid and skus and (run_at or not scheduled))
    if st.button("Review my request →", type="primary", disabled=not can_review, **WIDE):
        try:
            with st.spinner("Checking the current list…"):
                existing = backend.existing(skus, pid, full_table(pid))
            already = [s for s in skus if s in existing]
            missing = [s for s in skus if s not in existing]
            go, skip = (missing, already) if action == "add" else (already, missing)
            if scheduled:
                # State can change before the scheduled time, so keep every product in the job;
                # it is checked again when it runs. "skip" here is only for information.
                go = list(skus)
            st.session_state.df_review = dict(signature=signature, action=action, pid=pid,
                                           go=go, skip=skip, invalid=invalid,
                                           scheduled=scheduled, run_at=run_at, tz=sched_tz)
            st.session_state.pop("df_ack", None)
        except Exception as e:
            st.session_state.df_review = dict(signature=signature, error=str(e))
    if not can_review:
        st.caption("Choose a process, add at least one product number"
                   f"{' and a future US date & time' if scheduled else ''} to continue.")

    rv = st.session_state.df_review
    if rv and rv["signature"] == signature:
        step(4, "Review")
        if "error" in rv:
            st.error("We couldn't reach the data right now. Please try again, or contact the support team.")
            with st.expander("Details for the support team"):
                st.code(rv["error"])
        else:
            pname = PROCESS_CONFIG[rv["pid"]]["name"]
            is_add = rv["action"] == "add"
            go, skip, bad = rv["go"], rv["skip"], rv["invalid"]
            sched = rv.get("scheduled")
            when = f" on <b>{fmt_us(rv['run_at'], rv['tz'])}</b> ({fmt_ist(rv['run_at'])})" if sched else ""

            if go:
                verb = f"will be <b>excluded from {pname}</b>" if is_add else f"will be <b>brought back into {pname}</b>"
                st.markdown(f'<div class="summary-card card-go"><span class="big">{len(go)}</span>'
                            f'{"product" if len(go) == 1 else "products"} {verb}{when}.</div>', unsafe_allow_html=True)
            if skip and sched:
                why = f"already excluded from {pname}" if is_add else f"not currently excluded from {pname}"
                st.markdown(f'<div class="summary-card card-skip"><span class="big">{len(skip)}</span>'
                            f'{"is" if len(skip) == 1 else "are"} {why} right now — if that is still true at the '
                            f'scheduled time, nothing changes for {"it" if len(skip) == 1 else "them"}.</div>',
                            unsafe_allow_html=True)
            elif skip:
                why = f"already excluded from {pname}" if is_add else f"not currently excluded from {pname}"
                st.markdown(f'<div class="summary-card card-skip"><span class="big">{len(skip)}</span>'
                            f'{"is" if len(skip) == 1 else "are"} {why} — nothing to do.</div>', unsafe_allow_html=True)
            if bad:
                st.markdown(f'<div class="summary-card card-bad"><span class="big">{len(bad)}</span>'
                            f'{"entry was" if len(bad) == 1 else "entries were"} not a valid product number '
                            f'and will be ignored.</div>', unsafe_allow_html=True)

            if sched:
                at = fmt_us(rv["run_at"], rv["tz"])
                rows = ([{"Product": str(s), "What will happen": f"{'Exclude' if is_add else 'Bring back'} at {at}"
                          + (" (no change if still already done)" if s in skip else "")} for s in go] +
                        [{"Product": s, "What will happen": "Ignored (not a number)"} for s in bad])
            else:
                rows = ([{"Product": str(s), "What will happen": "Exclude" if is_add else "Bring back"} for s in go] +
                        [{"Product": str(s), "What will happen": "No change (already done)"} for s in skip] +
                        [{"Product": s, "What will happen": "Ignored (not a number)"} for s in bad])
            with st.expander(f"See the full list ({len(rows)})"):
                df = pd.DataFrame(rows)
                st.dataframe(df, hide_index=True, **WIDE)
                st.download_button("Download list", df.to_csv(index=False), "review.csv", "text/csv")

            if go and sched:
                step(5, "Confirm")
                at = fmt_us(rv["run_at"], rv["tz"])
                ack_text = (f"I want to schedule: {'exclude' if is_add else 'bring back'} {plural(len(go))} "
                            f"{'from' if is_add else 'into'} {pname} on **{at}**")
                if env == "prod":
                    ack_text += " on the **Live** system"
                ack = st.checkbox(ack_text, key="df_ack")
                btn = f"⏰ Schedule — {'exclude' if is_add else 'bring back'} {plural(len(go))}"
                if st.button(btn, type="primary", disabled=not ack, **WIDE):
                    if rv["run_at"] <= datetime.now(timezone.utc):
                        st.error("That time has already passed — please pick a later time and review again.")
                    else:
                        try:
                            job_id = "SCH-" + uuid.uuid4().hex[:8].upper()
                            backend.schedule(dict(job_id=job_id, action=rv["action"], process_id=rv["pid"],
                                                  skus=go, run_at=rv["run_at"], tz=rv["tz"], status="Scheduled",
                                                  created_by=CURRENT_USER,
                                                  created_at=datetime.now(timezone.utc)))
                            msg = (f"{plural(len(go))} will be {'excluded from' if is_add else 'brought back into'} "
                                   f"{pname} on **{at}** ({fmt_ist(rv['run_at'])}). "
                                   "You can see or cancel it in the **⏰ Scheduled** tab.")
                            now = datetime.now().strftime("%d %b %Y, %H:%M")
                            st.session_state.df_done = dict(message=msg, ref=job_id, time=now)
                            st.session_state.df_log.insert(0, {
                                "When": now, "System": ENV_LABEL[env],
                                "Change": f"Scheduled {'exclusion' if is_add else 'bring-back'} for {at}",
                                "Process": pname, "Products": len(go), "Reference": job_id,
                                "Product numbers": ", ".join(map(str, go)),
                            })
                            st.session_state.df_review = None
                            st.rerun()
                        except Exception as e:
                            st.error("Something went wrong and the change was not scheduled. Please try again.")
                            with st.expander("Details for the support team"):
                                st.code(str(e))
            elif go:
                step(5, "Confirm")
                ack_text = (f"I want to {'exclude' if is_add else 'bring back'} {plural(len(go))} "
                            f"{'from' if is_add else 'into'} {pname}")
                if env == "prod":
                    ack_text += " on the **Live** system"
                ack = st.checkbox(ack_text, key="df_ack")
                btn = f"{'Exclude' if is_add else 'Bring back'} {plural(len(go))}"
                if st.button(btn, type="primary", disabled=not ack, **WIDE):
                    try:
                        with st.spinner("Applying your change…"):
                            ref, n = backend.apply(rv["action"], go, rv["pid"], full_table(rv["pid"]))
                        msg = (f"{plural(n)} {'excluded from' if is_add else 'brought back into'} {pname}.")
                        now = datetime.now().strftime("%d %b %Y, %H:%M")
                        st.session_state.df_done = dict(message=msg, ref=ref, time=now)
                        st.session_state.df_log.insert(0, {
                            "When": now, "System": ENV_LABEL[env],
                            "Change": "Excluded" if is_add else "Brought back",
                            "Process": pname, "Products": n, "Reference": ref,
                            "Product numbers": ", ".join(map(str, go)),
                        })
                        st.session_state.df_review = None
                        st.rerun()
                    except Exception as e:
                        st.error("Something went wrong and the change was not applied. Please try again.")
                        with st.expander("Details for the support team"):
                            st.code(str(e))
            else:
                st.info("There's nothing to change — every product is already in the state you asked for.")
    elif rv:
        st.caption("You changed something above — click **Review my request** again.")

with tab_change:
    change_tab()

# --------------------------------------------------------------------------- #
# TAB 2 — Check a product
# --------------------------------------------------------------------------- #
with tab_check:
    st.write("See which processes a product is currently excluded from.")
    q = st.text_area("Product numbers to check", key="df_lookup", height=100,
                     placeholder="One or more product numbers")
    lskus, _, lbad = split_entries(q)
    if st.button("Check", disabled=not lskus):
        try:
            with st.spinner("Looking up…"):
                found = {pid: backend.existing(lskus, pid, full_table(pid)) for pid in PROCESS_CONFIG}
            table = []
            for s in lskus:
                row = {"Product": str(s)}
                for pid, cfg in PROCESS_CONFIG.items():
                    row[cfg["name"]] = "🚫 Excluded" if s in found[pid] else "✅ Included"
                table.append(row)
            st.dataframe(pd.DataFrame(table), hide_index=True, **WIDE)
            st.caption("🚫 Excluded = the process ignores this product.  ✅ Included = normal.")
        except Exception as e:
            st.error("We couldn't look that up right now. Please try again.")
            with st.expander("Details for the support team"):
                st.code(str(e))
    if lbad:
        st.caption(f"Ignored (not a number): {', '.join(lbad)}")

# --------------------------------------------------------------------------- #
# TAB 3 — Scheduled changes
# --------------------------------------------------------------------------- #
STATUS_ICON = {"Scheduled": "⏳ Scheduled", "Running": "🔄 Running", "Done": "✅ Done",
               "Failed": "❌ Failed", "Cancelled": "🚫 Cancelled"}

with tab_sched:
    st.write("Changes set to run at a future US date & time. They apply automatically when the time comes.")
    try:
        jobs = backend.list_jobs()
    except Exception as e:
        jobs = None
        st.error("We couldn't load the scheduled changes right now. Please try again.")
        with st.expander("Details for the support team"):
            st.code(str(e))
    if jobs is not None:
        pending = sorted([j for j in jobs if j["status"] == "Scheduled"], key=lambda j: j["run_at"])
        past = sorted([j for j in jobs if j["status"] != "Scheduled"], key=lambda j: j["run_at"], reverse=True)

        def _row(j):
            is_add = j["action"] == "add"
            return {
                "Reference": j["job_id"], "Runs at (US)": fmt_us(j["run_at"], j["tz"]),
                "India time": fmt_ist(j["run_at"]),
                "Change": "Exclude" if is_add else "Bring back",
                "Process": PROCESS_CONFIG.get(j["process_id"], {"name": j["process_id"]})["name"],
                "Products": len(j["skus"]), "Status": STATUS_ICON.get(j["status"], j["status"]),
                "Scheduled by": j.get("created_by") or "",
            }

        st.markdown(f"**Upcoming ({len(pending)})**")
        if not pending:
            st.info("Nothing scheduled. Choose **Schedule for a US time** in step 3 of *Make a change*.")
        else:
            st.dataframe(pd.DataFrame([_row(j) for j in pending]), hide_index=True, **WIDE)
            c1, c2 = st.columns([3, 1])
            to_cancel = c1.selectbox(
                "Cancel a scheduled change", [j["job_id"] for j in pending], index=None,
                placeholder="Pick a reference to cancel",
                format_func=lambda i: next(f"{i} — {r['Change']} {r['Products']} in {r['Process']} at "
                                           f"{r['Runs at (US)']}" for r in map(_row, pending) if r["Reference"] == i))
            c2.write("")
            c2.write("")
            if c2.button("Cancel it", disabled=not to_cancel, **WIDE):
                try:
                    ok = backend.cancel(to_cancel)
                    if ok:
                        st.session_state.df_log.insert(0, {
                            "When": datetime.now().strftime("%d %b %Y, %H:%M"), "System": ENV_LABEL[env],
                            "Change": "Cancelled scheduled change", "Process": "", "Products": 0,
                            "Reference": to_cancel, "Product numbers": "",
                        })
                        st.success(f"{to_cancel} cancelled — nothing will change.")
                        st.rerun()
                    else:
                        st.warning("That change has already run or was cancelled.")
                except Exception as e:
                    st.error("Couldn't cancel it right now. Please try again.")
                    with st.expander("Details for the support team"):
                        st.code(str(e))
            with st.expander("Product numbers in each upcoming change"):
                st.dataframe(pd.DataFrame([{"Reference": j["job_id"], "Product numbers": ", ".join(map(str, j["skus"]))}
                                           for j in pending]), hide_index=True, **WIDE)

        if past:
            with st.expander(f"Already run or cancelled ({len(past)})"):
                hist = pd.DataFrame([{**_row(j), "Products changed": j.get("applied_count"),
                                      "Result": j.get("result_ref") or ""} for j in past])
                st.dataframe(hist, hide_index=True, **WIDE)
        st.caption("Checked every minute while the app is open. "
                   + ("In practice mode, schedules last only while this browser tab stays open."
                      if BACKEND == "demo" else
                      "Schedules are saved in BigQuery; a change that comes due while nobody has the app "
                      "open is applied the next time anyone opens it."))

# --------------------------------------------------------------------------- #
# TAB 4 — Activity log
# --------------------------------------------------------------------------- #
with tab_log:
    if not st.session_state.df_log:
        st.info("Changes you make in this session will appear here.")
    else:
        df = pd.DataFrame(st.session_state.df_log)
        st.dataframe(df.drop(columns=["Product numbers"]), hide_index=True, **WIDE)
        st.download_button("Download activity log", df.to_csv(index=False), "activity_log.csv", "text/csv")

# --------------------------------------------------------------------------- #
# Technical details (hidden by default)
# --------------------------------------------------------------------------- #
with st.expander("Technical details"):
    st.caption(f"Data source: {'dummy in-memory tables' if BACKEND == 'demo' else 'BigQuery'}  ·  dataset `{dataset}`")
    st.dataframe(pd.DataFrame([{"Process": c["name"], "Table": c["table_name"]} for c in PROCESS_CONFIG.values()]
                              + [{"Process": "Scheduled changes", "Table": SCHEDULE_TABLE}]),
                 hide_index=True, **WIDE)
    if BACKEND == "demo" and st.button("Reset practice data"):
        st.session_state.df_demo_db.pop(env, None)
        st.session_state.get("df_demo_sched", {}).pop(env, None)
        st.session_state.df_review = None
        st.rerun()
