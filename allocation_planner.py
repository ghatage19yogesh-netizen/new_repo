"""
Allocation Planner — Streamlit app for planners (business users).

Step 1 · Review & adjust : load the latest optimizer plan, apply the monthly planner input
                           (storage types, min / max targets, building capacity, departments), check it.
Step 2 · Publish         : publish the plan for reporting (BigQuery) or download it as Excel.

Follows results_main.py of the scs-com-allocation pipeline. Demo mode simulates GCS and BigQuery.
Inside the portal: sign-in, home and sign-out are handled by streamlit_app.py.
Add users:  python streamlit_app.py adduser alloc <username> "<Full Name>" <password>
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import pickle
import re
import secrets as pysecrets
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(os.environ.get("ALLOC_DATA_DIR", Path(__file__).resolve().parent / "data"))
DEMO_DB = DATA_DIR / "bigquery_demo.sqlite"
PUB_LOG = DATA_DIR / "publications.json"


# ======================================================================================
# Auth — planner only
# ======================================================================================
def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


def make_user(name: str, password: str) -> dict:
    salt = pysecrets.token_hex(16)
    return {"name": name, "role": "planner", "salt": salt, "password_hash": hash_password(password, salt)}


_DEMO_SALT = "8f1d2c3b4a5968778695a4b3c2d1e0f1"
DEMO_USERS = {"yogesh": {"name": "Yogesh", "role": "planner", "salt": _DEMO_SALT,
                         "password_hash": hash_password("planner123", _DEMO_SALT)}}
DEMO_PASSWORD = "planner123"

if __name__ == "__main__" and len(sys.argv) >= 2 and sys.argv[1] == "adduser":
    if len(sys.argv) != 5:
        print('usage: python app.py adduser <username> "<Full Name>" <password>')
        sys.exit(1)
    _u, _n, _p = sys.argv[2:]
    print(f"[users.{_u}]")
    for _k, _v in make_user(_n, _p).items():
        print(f"{_k} = {json.dumps(_v)}")
    sys.exit(0)

import streamlit as st  # noqa: E402

if "PORTAL" not in globals():  # standalone run; inside the portal the page is configured by streamlit_app.py
    st.set_page_config(page_title="Allocation Planner", page_icon=":material/inventory_2:", layout="wide")
ss = st.session_state
for _k in ["src", "month", "run_id", "env", "model_version", "notes", "gcs_path", "inc_storage", "excl_depts", "excl_groups"]:  # keep values across pages
    if _k in ss:
        ss[_k] = ss[_k]


def _secrets() -> dict:
    try:
        return st.secrets
    except Exception:
        return {}


def load_users() -> tuple[dict, bool]:
    try:
        users = {k: dict(v) for k, v in _secrets()["users"].items()}
        users = {k: v for k, v in users.items() if v.get("role", "planner") == "planner"}
        if users:
            return users, False
    except Exception:
        pass
    return DEMO_USERS, True


def check_login(users, username, password):
    u = users.get((username or "").strip().lower())
    if not u:
        hash_password(password or "", "00" * 16)
        return None
    if hmac.compare_digest(hash_password(password or "", u["salt"]), u["password_hash"]):
        return {"username": username.strip().lower(), "name": u["name"]}
    return None


# ======================================================================================
# Demo optimizer output (static sample of results_output.pkl — no optimizer code here)
# ======================================================================================
DEMO_VERSION = 2
# Buildings, capacities and min / max penetration as in the planner input email (October).
DEMO_CFCS = [("10189", "Bailey Road"), ("11290", "China Grove"), ("9615", "Goodyear"),
             ("7972", "Joppa"), ("9609", "Portland"), ("9610", "Martinsburg (W. Virginia)")]
DEMO_LOCATIONS = dict(DEMO_CFCS)
_N = None  # "-" in the email = no space for that storage type
DEMO_CAPACITY = {  # storage type -> {CFC ID: capacity units}
    "GOH": {"10189": _N, "11290": 280_000, "9615": 446_000, "7972": _N, "9609": 872_000, "9610": 990_000},
    "HFG": {"10189": 1_134_000, "11290": 1_575_000, "9615": 1_144_000, "7972": _N, "9609": 300_000, "9610": 300_000},
    "PLR": {"10189": 712_000, "11290": 675_000, "9615": 758_000, "7972": _N, "9609": 200_000, "9610": 200_000},
    "SHO": {"10189": _N, "11290": 800_000, "9615": 475_000, "7972": _N, "9609": 966_000, "9610": 1_131_000},
    "BTR": {"10189": _N, "11290": 6_000_000, "9615": 2_664_000, "7972": _N, "9609": 3_872_000, "9610": 7_990_000},
    "FLA": {"10189": 200_000, "11290": 7_250_000, "9615": 6_285_000, "7972": _N, "9609": 10_990_000, "9610": 10_769_000},
    "TT": {"10189": _N, "11290": _N, "9615": 500_000, "7972": 950_000, "9609": _N, "9610": _N},
    "JWL": {"10189": _N, "11290": _N, "9615": _N, "7972": _N, "9609": _N, "9610": _N},  # jewelry (demo)
}
DEMO_LIMITS = {
    "min_pct": {"10189": 2.0, "11290": 10.0, "9615": 21.0, "7972": 0.0, "9609": 25.0, "9610": 26.0},
    "max_pct": {"10189": 5.0, "11290": 15.0, "9615": 25.0, "7972": 99.0, "9609": 30.0, "9610": 33.0},
}
# Bailey Road: FLA capacity is pooled with HFG
DEMO_POOLED = [("10189", "FLA+HFG", 1_600_000)]
# Departments (number, name, group, storage). Names other than D137 / D642 / D753 are demo values.
DEMO_DEPTS = [("101", "MEN'S TOPS", "Apparel", "FLA"), ("110", "WOMEN'S DRESSES", "Apparel", "GOH"),
              ("150", "COATS", "Apparel", "GOH"), ("120", "SHOES", "Shoes", "SHO"),
              ("130", "HANDBAGS", "Accessories", "PLR"), ("260", "FINE JEWELRY", "Jewelry", "JWL"),
              ("137", "CELEBRATION", "Home", "HFG"), ("640", "TOP OF TABLE", "Home", "TT"),
              ("642", "TRIM A HOME", "Home", "BTR"), ("753", "PB TRIM A HOME", "Home", "BTR"),
              ("645", "BED & BATH", "Home", "PLR"), ("648", "HOUSEWARES", "Home", "HFG"),
              ("210", "FURNITURE", "Big Ticket", "BTR"), ("215", "MATTRESSES", "Big Ticket", "BTR"),
              ("660", "GOURMET FOOD", "Food", "FLA")]
DEMO_EXCLUDED_GROUPS = ["Big Ticket", "Food"]  # "currently on the exclusion list"
DEMO_ZONES = ["Northeast", "Southeast", "Central", "West"]


def _build_demo_allocation():
    """Static sample of the optimizer's output, sized to the capacities above (not an optimizer)."""
    rng = np.random.default_rng(7)
    zone_w = {"10189": [.25, .35, .3, .1], "11290": [.2, .45, .25, .1], "9615": [.05, .15, .25, .55],
              "7972": [.6, .2, .15, .05], "9609": [.05, .05, .2, .7], "9610": [.55, .25, .15, .05]}
    dist = {"10189": [3.2, 2.6, 3.0, 5.4], "11290": [3.0, 2.2, 3.1, 5.6], "9615": [5.8, 4.6, 3.6, 2.3],
            "7972": [2.3, 3.0, 3.6, 5.9], "9609": [6.0, 5.6, 4.0, 2.4], "9610": [2.2, 2.9, 3.4, 5.8]}
    util = {"10189": .7, "11290": .22, "9615": .62, "7972": .7, "9609": .55, "9610": .5}
    jwl_cap = {"9615": 60_000, "9610": 80_000}  # jewelry units in the demo output (no capacity row)
    prods = {}
    for d, _, _, k in DEMO_DEPTS:
        for v, vel in ((1001, "high"), (2002, "medium" if k != "BTR" else "low")):
            prods.setdefault(k, []).append(f"{d}-{v}-{vel}")
    rows = []
    for k, plist in prods.items():
        caps = jwl_cap if k == "JWL" else {w: c for w, c in DEMO_CAPACITY[k].items() if c}
        for w, cap in caps.items():
            total = cap * (util[w] if k != "JWL" else .7)
            if w == "10189" and k in ("FLA", "HFG"):
                total = cap * .62
            pw = rng.dirichlet(np.ones(len(plist)) * 2)
            for p, share in zip(plist, pw):
                for z, zw, dc in zip(DEMO_ZONES, zone_w[w], dist[w]):
                    u = round(total * share * zw)
                    if u <= 0:
                        continue
                    uc = round(dc * (1.4 if k in ("BTR", "TT") else 1.0) * rng.uniform(.9, 1.1), 2)
                    rows.append((w, z, p, k, float(u), uc, round(u * uc, 2),
                                 "Slow seller" if p.endswith("low") else "Optimizer"))
    return rows


DEMO_ALLOCATION = _build_demo_allocation()  # warehouse, zone, product_type, storage, units, unit_cost, cost, method

REQUIRED = ["warehouse", "zone", "product_type", "storage", "units"]
OPTIONAL = ["unit_cost", "cost", "method"]

BQ_TABLES = {
    "allocation_plan": {
        "desc": "Units to stock per warehouse × zone × product type, with cost and how it was decided.",
        "schema": [("run_id", "STRING"), ("target_month", "DATE"), ("warehouse_id", "STRING"), ("location", "STRING"), ("zone", "STRING"),
                   ("product_type", "STRING"), ("dept_nbr", "STRING"), ("dept_name", "STRING"), ("storage", "STRING"), ("units", "FLOAT64"),
                   ("unit_cost", "FLOAT64"), ("cost", "FLOAT64"), ("method", "STRING"),
                   ("published_at", "TIMESTAMP")]},
    "capacity_summary": {
        "desc": "Space used per warehouse × storage type, with overflow above capacity.",
        "schema": [("run_id", "STRING"), ("target_month", "DATE"), ("warehouse_id", "STRING"), ("location", "STRING"), ("storage", "STRING"),
                   ("allocated", "FLOAT64"), ("capacity", "FLOAT64"), ("used_pct", "FLOAT64"),
                   ("overflow", "FLOAT64"), ("published_at", "TIMESTAMP")]},
    "warehouse_share": {
        "desc": "Each warehouse (ID + location) and its share of all units vs its min / max target.",
        "schema": [("run_id", "STRING"), ("target_month", "DATE"), ("warehouse_id", "STRING"), ("location", "STRING"), ("units", "FLOAT64"),
                   ("share_pct", "FLOAT64"), ("min_pct", "FLOAT64"), ("max_pct", "FLOAT64"),
                   ("published_at", "TIMESTAMP")]},
    "run_log": {
        "desc": "One row per publish: run parameters, adjustments made in the app, totals, source and who published it.",
        "schema": [("run_id", "STRING"), ("target_month", "DATE"), ("pipeline_env", "STRING"),
                   ("model_version", "STRING"), ("solver_status", "STRING"), ("source", "STRING"),
                   ("total_units", "FLOAT64"), ("total_cost", "FLOAT64"), ("overflow_units", "FLOAT64"),
                   ("row_count", "INT64"), ("notes", "STRING"), ("adjustments_json", "STRING"), ("published_by", "STRING"),
                   ("published_at", "TIMESTAMP")]},
}
_ID_PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_ID_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def demo_frames() -> dict:
    cap = pd.DataFrame(DEMO_CAPACITY).rename_axis("warehouse").reset_index().melt(
        id_vars="warehouse", var_name="storage", value_name="capacity")
    lim = pd.DataFrame(DEMO_LIMITS).rename_axis("warehouse").reset_index()
    pooled = pd.DataFrame(DEMO_POOLED, columns=["warehouse_id", "storage_types", "pooled_capacity"])
    depts = pd.DataFrame([d[:3] for d in DEMO_DEPTS], columns=["dept_nbr", "dept_name", "dept_group"])
    return {"capacity": cap, "limits": lim, "pooled": pooled, "departments": depts,
            "dept_exclusions": {"groups": list(DEMO_EXCLUDED_GROUPS), "departments": []}}


def demo_output() -> dict:
    alloc = pd.DataFrame(DEMO_ALLOCATION, columns=REQUIRED[:4] + ["units", "unit_cost", "cost", "method"])
    return {"allocation": alloc, **demo_frames(), "locations": dict(DEMO_LOCATIONS), "solver_status": "Optimal",
            "source": "Demo output (sample results_output.pkl)"}


# ======================================================================================
# Reading optimizer output files
# ======================================================================================
def _norm(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    alias = {"loc": "warehouse", "cfc": "warehouse", "dest": "zone", "destination": "zone", "prod_type": "product_type",
             "strg_type": "storage", "storage_type": "storage", "supply": "units", "allocation": "units"}
    return df.rename(columns={k: v for k, v in alias.items() if k in df.columns and v not in df.columns})


def read_table_file(f) -> pd.DataFrame:
    name = f.name.lower()
    if name.endswith(".csv"):
        return _norm(pd.read_csv(f))
    if name.endswith((".xlsx", ".xls")):
        return _norm(pd.read_excel(f))
    raise ValueError("Use a .csv or .xlsx file")


def read_results_pickle(data: bytes) -> dict:
    """Accepts a DataFrame, or a dict / tuple holding DataFrames (allocation + optional capacity / limits)."""
    obj = pickle.loads(data)  # only for files from your own pipeline
    out = {}
    if isinstance(obj, pd.DataFrame):
        out["allocation"] = _norm(obj)
    elif isinstance(obj, dict):
        for key in ("allocation", "detail", "allocation_plan", "results", "final_results"):
            if isinstance(obj.get(key), pd.DataFrame):
                out["allocation"] = _norm(obj[key]); break
        for key in ("capacity", "storage_capacity", "new_storage_cap"):
            if isinstance(obj.get(key), pd.DataFrame):
                out["capacity"] = _norm(obj[key]); break
        if isinstance(obj.get("limits"), pd.DataFrame):
            out["limits"] = _norm(obj["limits"])
        k = obj.get("kpis") or {}
        out["solver_status"] = k.get("status") or obj.get("status")
    elif isinstance(obj, (list, tuple)):
        dfs = [x for x in obj if isinstance(x, pd.DataFrame)]
        if dfs:
            out["allocation"] = _norm(dfs[0])
    if "allocation" not in out:
        raise ValueError("No allocation table found in the pickle (expected a DataFrame or a dict with "
                         "'allocation' / 'detail').")
    return out


def validate_output(out: dict) -> list[dict]:
    a = out.get("allocation")
    res = []
    if a is None or a.empty:
        return [{"level": "error", "text": "No allocation rows"}]
    miss = [c for c in REQUIRED if c not in a.columns]
    res.append({"level": "error" if miss else "ok",
                "text": "Required columns present" if not miss else f"Missing columns: {', '.join(miss)}"})
    if miss:
        return res
    units = pd.to_numeric(a["units"], errors="coerce")
    bad = int(units.isna().sum())
    res.append({"level": "error" if bad else "ok", "text": "Units are numbers" if not bad else f"{bad} non-numeric units"})
    neg = int((units < 0).sum())
    res.append({"level": "error" if neg else "ok", "text": "No negative units" if not neg else f"{neg} negative units"})
    nk = int(a[["warehouse", "product_type", "storage"]].isna().sum().sum())
    res.append({"level": "error" if nk else "ok",
                "text": "No missing warehouse / product / storage" if not nk else f"{nk} missing key values"})
    dup = int(a.duplicated(subset=["warehouse", "zone", "product_type"]).sum())
    res.append({"level": "warn" if dup else "ok",
                "text": "No duplicate warehouse × zone × product rows" if not dup else f"{dup} duplicate rows"})
    res.append({"level": "ok" if out.get("capacity") is not None else "warn",
                "text": "Capacity included" if out.get("capacity") is not None
                else "No capacity file — capacity_summary will show allocation only"})
    st_ = out.get("solver_status")
    if st_:
        res.append({"level": "ok" if st_ == "Optimal" else "warn", "text": f"Solver status: {st_}"})
    return res


def derive_tables(out: dict, meta: dict, published_by: str, published_at: str) -> dict:
    a = out["allocation"].copy()
    a["units"] = pd.to_numeric(a["units"], errors="coerce").fillna(0)
    for c in OPTIONAL:
        if c not in a.columns:
            a[c] = None
    if a["cost"].isna().all() and a["unit_cost"].notna().any():
        a["cost"] = pd.to_numeric(a["unit_cost"], errors="coerce") * a["units"]
    common = {"run_id": meta["run_id"], "target_month": meta["target_month"], "published_at": published_at}

    locs = out.get("locations") or {}
    dmaster = out.get("departments")
    dept_names = dict(dmaster[["dept_nbr", "dept_name"]].astype(str).values) if dmaster is not None else {}

    def frame(df, name):
        df = df.copy()
        if "warehouse" in df.columns:
            df["warehouse_id"] = df["warehouse"].astype(str)
            df["location"] = df["warehouse_id"].map(locs)
        if "product_type" in df.columns:
            df["dept_nbr"] = df["product_type"].astype(str).str.split("-").str[0]
            df["dept_name"] = df["dept_nbr"].map(dept_names)
        for k, v in common.items():
            df[k] = v
        cols = [c for c, _ in BQ_TABLES[name]["schema"]]
        for c in cols:
            if c not in df.columns:
                df[c] = None
        return df[cols].reset_index(drop=True)

    alloc_ws = a.groupby(["warehouse", "storage"], as_index=False)["units"].sum().rename(columns={"units": "allocated"})
    if out.get("capacity") is not None:
        cap = out["capacity"][["warehouse", "storage", "capacity"]]
        cs = alloc_ws.merge(cap, on=["warehouse", "storage"], how="outer").fillna({"allocated": 0})
        capn = pd.to_numeric(cs["capacity"], errors="coerce")
        cs["used_pct"] = (100 * cs["allocated"] / capn.where(capn > 0)).round(1)
        cs["overflow"] = (cs["allocated"] - cs["capacity"]).clip(lower=0).round(1)
    else:
        cs = alloc_ws.assign(capacity=None, used_pct=None, overflow=None)
    pooled = out.get("pooled")
    if pooled is not None and len(pooled):
        extra = []
        for w, kinds, pcap in pooled[["warehouse_id", "storage_types", "pooled_capacity"]].values:
            ks = [x.strip().upper() for x in str(kinds).split("+") if x.strip()]
            al = float(alloc_ws[(alloc_ws["warehouse"] == str(w)) & alloc_ws["storage"].isin(ks)]["allocated"].sum())
            pc = float(pcap) if pd.notna(pcap) else None
            extra.append({"warehouse": str(w), "storage": "+".join(ks) + " (pooled)", "allocated": al, "capacity": pc,
                          "used_pct": round(100 * al / pc, 1) if pc else None,
                          "overflow": round(max(al - pc, 0), 1) if pc else 0.0})
        cs = pd.concat([cs, pd.DataFrame(extra)], ignore_index=True)
    tot = a["units"].sum()
    sh = a.groupby("warehouse", as_index=False)["units"].sum()
    sh["share_pct"] = (100 * sh["units"] / tot).round(1) if tot else 0
    if out.get("limits") is not None:
        lim = out["limits"].assign(warehouse=out["limits"]["warehouse"].astype(str))
        sh = sh.merge(lim[["warehouse", "min_pct", "max_pct"]], on="warehouse", how="left")
    overflow = float(pd.to_numeric(cs.loc[~cs["storage"].astype(str).str.endswith("(pooled)"), "overflow"],
                                   errors="coerce").fillna(0).sum())
    log = pd.DataFrame([{**common, "pipeline_env": meta["env"], "model_version": meta["model_version"],
                         "solver_status": out.get("solver_status") or "Unknown", "source": out.get("source"),
                         "total_units": round(float(tot), 2),
                         "total_cost": round(float(pd.to_numeric(a["cost"], errors="coerce").fillna(0).sum()), 2),
                         "overflow_units": overflow, "row_count": len(a), "notes": meta["notes"],
                         "adjustments_json": json.dumps(meta.get("adjustments", {})),
                         "published_by": published_by}])
    return {"allocation_plan": frame(a, "allocation_plan"), "capacity_summary": frame(cs, "capacity_summary"),
            "warehouse_share": frame(sh, "warehouse_share"), "run_log": frame(log, "run_log")}


# ======================================================================================
# BigQuery writers — demo (SQLite) and connected (google-cloud-bigquery)
# ======================================================================================
def bq_live() -> bool:
    try:
        return "gcp_service_account" in _secrets()
    except Exception:
        return False


def demo_publish(tables, dataset, names, month, mode) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rows = {}
    with sqlite3.connect(DEMO_DB) as con:
        for key, df in tables.items():
            t = f"{dataset}__{names[key]}"
            if mode == "replace" and con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
                con.execute(f'DELETE FROM "{t}" WHERE target_month = ?', (month,))
            df.to_sql(t, con, if_exists="append", index=False)
            rows[key] = len(df)
    return rows


def demo_inventory(dataset) -> pd.DataFrame:
    if not DEMO_DB.exists():
        return pd.DataFrame()
    out = []
    with sqlite3.connect(DEMO_DB) as con:
        for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?",
                                (f"{dataset}__%",)).fetchall():
            c, m, r = con.execute(f'SELECT COUNT(*), COUNT(DISTINCT target_month), COUNT(DISTINCT run_id) '
                                  f'FROM "{n}"').fetchone()
            out.append({"Table": f"{dataset}.{n.split('__', 1)[1]}", "Rows": c, "Months": m, "Runs": r})
    return pd.DataFrame(out)


def demo_query(sql, project, dataset) -> pd.DataFrame:
    q = sql.strip().rstrip(";")
    if not re.match(r"(?is)^\s*(select|with)\b", q) or ";" in q:
        raise ValueError("Only a single SELECT query is allowed here.")
    q = q.replace("`", "")
    q = re.sub(rf"\b{re.escape(project)}\.{re.escape(dataset)}\.(\w+)", rf"{dataset}__\1", q)
    q = re.sub(rf"\b{re.escape(dataset)}\.(\w+)", rf"{dataset}__\1", q)
    con = sqlite3.connect(f"file:{DEMO_DB}?mode=ro", uri=True)
    try:
        return pd.read_sql_query(q, con)
    finally:
        con.close()


def bq_client(project, location):
    from google.cloud import bigquery
    from google.oauth2 import service_account
    info = dict(_secrets()["gcp_service_account"])
    creds = service_account.Credentials.from_service_account_info(info)
    return bigquery.Client(project=project or info.get("project_id"), credentials=creds, location=location)


def bq_publish(client, tables, project, dataset, names, month, mode) -> dict:
    from google.cloud import bigquery
    client.create_dataset(bigquery.Dataset(f"{project}.{dataset}"), exists_ok=True)
    rows = {}
    for key, df in tables.items():
        table_id = f"{project}.{dataset}.{names[key]}"
        schema = [bigquery.SchemaField(n, t) for n, t in BQ_TABLES[key]["schema"]]
        tbl = bigquery.Table(table_id, schema=schema)
        tbl.time_partitioning = bigquery.TimePartitioning(type_=bigquery.TimePartitioningType.MONTH,
                                                          field="target_month")
        tbl.description = BQ_TABLES[key]["desc"]
        client.create_table(tbl, exists_ok=True)
        if mode == "replace":
            client.query(f"DELETE FROM `{table_id}` WHERE target_month = @m",
                         job_config=bigquery.QueryJobConfig(query_parameters=[
                             bigquery.ScalarQueryParameter("m", "DATE", month)])).result()
        df = df.copy()
        df["target_month"] = pd.to_datetime(df["target_month"]).dt.date
        df["published_at"] = pd.to_datetime(df["published_at"])
        job = client.load_table_from_dataframe(df, table_id, job_config=bigquery.LoadJobConfig(
            schema=schema, write_disposition=bigquery.WriteDisposition.WRITE_APPEND))
        job.result()
        rows[key] = job.output_rows
    return rows


def read_pub_log() -> list:
    try:
        return json.loads(PUB_LOG.read_text())
    except Exception:
        return []


def add_pub_log(entry: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log = read_pub_log() + [entry]
    PUB_LOG.write_text(json.dumps(log, indent=1, default=str))


# ======================================================================================
# UI
# ======================================================================================
def month_options():
    t = date.today()
    return [date(t.year + (t.month - 1 + i) // 12, (t.month - 1 + i) % 12 + 1, 1).strftime("%B %Y")
            for i in range(-2, 4)]


LANDING_CSS = """
<style>
.lp-wrap{max-width:1080px;margin:0 auto}
.lp-eyebrow{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#5E5E5A;font-weight:600;margin:0 0 14px}
.lp-h1{font-size:clamp(34px,5vw,54px);line-height:1.05;font-weight:700;letter-spacing:-.02em;margin:0 0 18px;color:#1C1C1A}
.lp-h1 em{font-style:normal;color:#C26A1B}
.lp-lead{font-size:18px;line-height:1.55;color:#3D3D3A;max-width:640px;margin:0 0 8px}
.lp-flow{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:0;border:1.5px solid #1C1C1A;border-radius:8px;overflow:hidden;margin:8px 0 0;background:#fff}
.lp-node{padding:18px 18px 16px;border-right:1.5px solid #1C1C1A;position:relative}
.lp-node:last-child{border-right:0}
.lp-node.on{background:#1C1C1A;color:#fff}
.lp-node .k{font-size:12px;letter-spacing:.06em;text-transform:uppercase;opacity:.7;margin-bottom:6px}
.lp-node .t{font-weight:600;font-size:16px;margin-bottom:4px}
.lp-node .d{font-size:13.5px;line-height:1.45;opacity:.85}
.lp-sec{font-size:22px;font-weight:700;margin:0 0 4px;color:#1C1C1A}
.lp-sub{color:#5E5E5A;margin:0 0 18px}
.lp-steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:18px}
.lp-step{background:#fff;border:1.5px solid #C9C8C2;border-radius:8px;padding:20px}
.lp-num{display:inline-flex;width:30px;height:30px;border-radius:50%;background:#C26A1B;color:#fff;font-weight:700;align-items:center;justify-content:center;margin-bottom:10px}
.lp-step h3{font-size:17px;margin:0 0 6px;color:#1C1C1A}
.lp-step p{margin:0;color:#3D3D3A;font-size:14.5px;line-height:1.5}
.lp-feat{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:0;border-top:1.5px solid #1C1C1A}
.lp-f{padding:18px 18px 18px 0;border-bottom:1px solid #D9D8D2}
.lp-f b{display:block;font-size:15.5px;margin-bottom:4px;color:#1C1C1A}
.lp-f span{color:#3D3D3A;font-size:14px;line-height:1.5}
@media (max-width:640px){.lp-flow{grid-template-columns:1fr}.lp-node{border-right:0;border-bottom:1.5px solid #1C1C1A}.lp-node:last-child{border-bottom:0}}
.lp-tbl{width:100%;border-collapse:collapse;font-size:14px;background:#fff;border:1.5px solid #C9C8C2;border-radius:8px;overflow:hidden}
.lp-tbl th{text-align:left;font-weight:600;padding:10px 14px;border-bottom:1.5px solid #C9C8C2;background:#F4F3EF}
.lp-tbl td{padding:10px 14px;border-bottom:1px solid #E2E1DC;vertical-align:top}
.lp-tbl code{font-size:13px}
.lp-foot{color:#5E5E5A;font-size:13px;border-top:1px solid #D9D8D2;padding-top:14px;margin-top:8px}
</style>
"""


def _go_login():
    ss["view"] = "login"


def landing_page():
    st.markdown(LANDING_CSS, unsafe_allow_html=True)
    top_l, top_r = st.columns([4, 1])
    top_l.markdown("**Allocation Planner**")
    top_r.button("Sign in", key="lp_top_signin", on_click=_go_login, width="stretch")

    st.write("")
    left, right = st.columns([1.35, 1], gap="large")
    with left:
        st.markdown(
            '<div class="lp-wrap"><p class="lp-eyebrow">Monthly COM inventory allocation</p>'
            '<h1 class="lp-h1">Review the monthly plan, make your changes, <em>publish</em>.</h1>'
            '<p class="lp-lead">Check where inventory is going, apply this month\'s planner input — storage types, '
            'min / max targets, building capacity and departments — and share the final plan with reports and the '
            'warehouse teams.</p></div>', unsafe_allow_html=True)
        b1, b2, _ = st.columns([1, 1, 1.2])
        b1.button("Sign in as planner", type="primary", key="lp_signin", on_click=_go_login, width="stretch")
        b2.link_button("How it works ↓", "#how-it-works", width="stretch")
    with right:
        st.write("")
        st.markdown(
            '<div class="lp-flow" role="list" aria-label="How the plan flows">'
            '<div class="lp-node" role="listitem"><div class="k">Planning model</div><div class="t">Monthly plan</div>'
            '<div class="d">Where each product should be stocked</div></div>'
            '<div class="lp-node on" role="listitem"><div class="k">You</div><div class="t">Review &amp; adjust</div>'
            '<div class="d">Apply this month\'s input, check it</div></div>'
            '<div class="lp-node" role="listitem"><div class="k">Shared</div><div class="t">Published plan</div>'
            '<div class="d">Ready for reports and teams</div></div></div>',
            unsafe_allow_html=True)

    st.write(""); st.write("")
    st.markdown('<div id="how-it-works"></div><h2 class="lp-sec">How it works</h2>'
                '<p class="lp-sub">Two steps.</p>'
                '<div class="lp-steps">'
                '<div class="lp-step"><span class="lp-num">1</span><h3>Review &amp; adjust</h3>'
                '<p>See the latest plan by building and department. Enter the changes from the planner input '
                'and the plan updates straight away.</p></div>'
                '<div class="lp-step"><span class="lp-num">2</span><h3>Publish</h3>'
                '<p>Check the summary and publish the plan for reporting — or download it as Excel.</p></div>'
                '</div>', unsafe_allow_html=True)

    st.write(""); st.write("")
    st.markdown('<h2 class="lp-sec">What you can change</h2><p class="lp-sub">The four questions from the '
                'monthly planner input.</p><div class="lp-feat">'
                '<div class="lp-f"><b>1 · Storage types</b><span>Include or exclude a storage type (e.g. jewelry), '
                'or add a new one.</span></div>'
                '<div class="lp-f"><b>2 · Min / max targets</b><span>Set each building\'s share of units, shown by '
                'building name and ID.</span></div>'
                '<div class="lp-f"><b>3 · Building capacity</b><span>Edit space per building and storage type, '
                'including shared limits like Bailey Road FLA + HFG.</span></div>'
                '<div class="lp-f"><b>4 · Departments</b><span>Leave out groups such as Big Ticket and Food, or '
                'single departments.</span></div>'
                '<div class="lp-f"><b>Built-in checks</b><span>Flags buildings outside their targets, over '
                'capacity, or a plan that was already published.</span></div>'
                '<div class="lp-f"><b>History</b><span>See who published which month, and every change they '
                'made.</span></div>'
                '</div>', unsafe_allow_html=True)

    st.write("")
    c1, c2 = st.columns([3, 1])
    c1.markdown('<p class="lp-foot">Demo version — sample data; nothing is sent outside the app.</p>',
                unsafe_allow_html=True)
    c2.button("Sign in", type="primary", key="lp_bottom_signin", on_click=_go_login, width="stretch")


def login_page():
    users, demo = load_users()
    _, mid, _ = st.columns([1, 1.1, 1])
    with mid:
        st.write(""); st.write("")
        st.button("← Back", key="login_back", on_click=lambda: ss.update(view="landing"))
        st.markdown("## Allocation Planner")
        st.caption("Monthly allocation plan · planner sign-in")
        with st.form("login", border=True):
            u = st.text_input("Username", placeholder="e.g. yogesh")
            p = st.text_input("Password", type="password")
            ok = st.form_submit_button("Sign in", type="primary", width="stretch")
        if ok:
            user = check_login(users, u, p)
            if user:
                ss["user"] = user
                st.rerun()
            st.error("Username or password is incorrect.")
        if demo:
            with st.expander("Demo account"):
                st.markdown(f"Planner · username `yogesh` · password `{DEMO_PASSWORD}`")


# ======================================================================================
# Pipeline stages — queries_main.py → model_main.py → results_main.py (demo)
#   GCS and BigQuery are simulated locally. The optimizer is NOT in this app: in demo mode
#   model_main's run_optimization step returns a stored solution (the demo results_output.pkl).
# ======================================================================================
import time as _time

PIPELINE_CONFIG = {"PIPELINE_NAME": "scs-com-allocation", "DEPLOY_ENV": "prod", "GCP_PROJECT": "[gcp-project]",
                   "BUCKET_NAME": "[allocation-bucket]", "PROJECT_INTERIM_FOLDER": "interim",
                   "PROJECT_PATH": "gs://[allocation-bucket]/scs-com-allocation"}
GCS_DIR = DATA_DIR / "gcs" / "interim"
GS_PREFIX = f"gs://{PIPELINE_CONFIG['BUCKET_NAME']}/{PIPELINE_CONFIG['PROJECT_INTERIM_FOLDER']}"
DEPT_CLASS = {"101": "Tops", "202": "Dresses & suits", "305": "Outerwear", "410": "Accessories",
              "415": "Accessories", "520": "Hosiery", "610": "Jewellery"}
DEMO_BOOL_PARAMS = [("update_data", True, "Re-run the SQL and rebuild intermediate tables"),
                    ("use_medium_velocity_constraint", True, "Limit medium-velocity products to 2 buildings"),
                    ("allocate_low_velocity_by_rule", True, "Place slow sellers by rule before the LP"),
                    ("relax_capacity_when_short", True, "Soft capacity (with penalty) when demand > space")]
DEMO_INT_PARAMS = [("demand_lookback_months", 6, "Months of sales history used for demand"),
                   ("max_buildings_medium_vel", 2, "Max buildings for a medium-velocity product"),
                   ("medium_vel_max_min_units", 7, "min_units threshold for medium velocity"),
                   ("solver_time_limit_min", 30, "Gurobi time limit")]
_UNUSED_QUERY_PIPELINE = [("01_sales_history", "Sales by product type × zone × month", 162),
                  ("02_demand_forecast", "Demand per product type × zone for the target month", 27),
                  ("03_ship_costs", "Cost per unit for each CFC × zone × product type", 80),
                  ("04_cfc_capacity", "Space per CFC × storage type", 6),
                  ("05_product_attributes", "Storage type, velocity, class per product type", 9),
                  ("06_allocation_restrictions", "Must-be-in / forbidden-in rules", 2),
                  ("07_cfc_alloc_limits", "Min / max share per CFC", 3)]


import threading as _threading
_GCS_LOCK = _threading.RLock()  # Streamlit Cloud runs all sessions in one process: serialise demo-file changes


def _gcs_put(name: str, data: bytes):
    GCS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = GCS_DIR / f".{name}.{os.getpid()}.{_threading.get_ident()}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, GCS_DIR / name)  # atomic: readers never see a half-written or missing file


def _gcs_get(name: str) -> bytes:
    p = GCS_DIR / name
    if not p.exists():
        raise FileNotFoundError(f"{GS_PREFIX}/{name} not found")
    return p.read_bytes()


def _gcs_exists(name: str) -> bool:
    return (GCS_DIR / name).exists()


# ---------- demo inputs: what queries_main.py / model_main.py leave in GCS ----------
def make_dataset_demo(month_iso, bool_params, int_params):
    """Demo make_dataset(): the 4 objects saved in query_parameters.pkl."""
    alloc = pd.DataFrame(DEMO_ALLOCATION, columns=REQUIRED[:4] + ["units", "unit_cost", "cost", "method"])
    prods = sorted(alloc["product_type"].unique())
    sets = {"cfcs": sorted(DEMO_LOCATIONS), "destinations": sorted(alloc["zone"].unique()),
            "storages": sorted(DEMO_CAPACITY), "product_types": prods,
            "dept_groups": sorted({d[2] for d in DEMO_DEPTS})}
    dem = alloc.groupby(["zone", "product_type"])["units"].sum()
    params = {"target_month": month_iso,
              "demand": {k: float(v) for k, v in dem.items()},
              "costs": {(w, z, p): float(c) for w, z, p, c in
                        alloc.dropna(subset=["unit_cost"])[["warehouse", "zone", "product_type", "unit_cost"]].values},
              "product_storage": dict(alloc.drop_duplicates("product_type")[["product_type", "storage"]].values),
              "cfc_location": dict(DEMO_LOCATIONS)}
    options = {"bool_params": bool_params, "int_params": int_params, "deploy_env": PIPELINE_CONFIG["DEPLOY_ENV"],
               "demo_version": DEMO_VERSION}
    f = demo_frames()
    constraints = {"storage_capacity": f["capacity"], "alloc_limits": f["limits"], "pooled_capacity": f["pooled"],
                   "departments": f["departments"], "dept_exclusions": f["dept_exclusions"],
                   "exceptions": pd.DataFrame([("640-1001-high", "must_be_in", "7972")],
                                              columns=["product_type", "rule", "cfcs"])}
    return (sets, params, options, constraints)


def ensure_demo_gcs(month_label: str):
    """Demo: place the files the pipeline would leave in GCS (query_parameters.pkl, results_output.pkl)."""
    with _GCS_LOCK:
        _ensure_demo_gcs(month_label)


def _ensure_demo_gcs(month_label: str):
    month_iso = datetime.strptime(month_label, "%B %Y").date().isoformat()
    try:
        stale = pickle.loads(_gcs_get("query_parameters.pkl"))[2].get("demo_version") != DEMO_VERSION
    except Exception:
        stale = True
    if stale:
        for n in ("query_parameters.pkl", "results_output.pkl"):
            if _gcs_exists(n):
                (GCS_DIR / n).unlink(missing_ok=True)
    if not _gcs_exists("query_parameters.pkl"):
        clf = make_dataset_demo(month_iso, {k: v for k, v, _ in DEMO_BOOL_PARAMS},
                                {k: v for k, v, _ in DEMO_INT_PARAMS})
        _gcs_put("query_parameters.pkl", pickle.dumps(clf))
    if not _gcs_exists("results_output.pkl"):
        alloc = pd.DataFrame(DEMO_ALLOCATION, columns=REQUIRED[:4] + ["units", "unit_cost", "cost", "method"])
        _gcs_put("results_output.pkl", pickle.dumps({
            "detail": alloc, "target_month": month_iso,
            "kpis": {"status": "Optimal", "units": float(alloc["units"].sum()), "total_cost": float(alloc["cost"].sum())}}))


def load_pipeline_output() -> dict:
    """results_main.py: download query_parameters.pkl (constraints = clf[3]) and results_output.pkl."""
    with _GCS_LOCK:
        try:
            clf = pickle.loads(_gcs_get("query_parameters.pkl"))
            results = pickle.loads(_gcs_get("results_output.pkl"))
        except (FileNotFoundError, EOFError, pickle.UnpicklingError):
            # demo files missing or half-written (restart, another session, cleared disk): rebuild and retry once
            for n in ("query_parameters.pkl", "results_output.pkl"):
                (GCS_DIR / n).unlink(missing_ok=True)
            _ensure_demo_gcs(ss.get("month") or month_options()[2])
            clf = pickle.loads(_gcs_get("query_parameters.pkl"))
            results = pickle.loads(_gcs_get("results_output.pkl"))
    constraints = clf[3]
    out = {"allocation": results["detail"].copy(), "capacity": constraints["storage_capacity"].copy(),
           "limits": constraints["alloc_limits"].copy(), "locations": dict(clf[1].get("cfc_location", {})),
           "pooled": constraints.get("pooled_capacity"), "departments": constraints.get("departments"),
           "dept_exclusions": constraints.get("dept_exclusions"),
           "solver_status": results["kpis"]["status"], "source": f"{GS_PREFIX}/results_output.pkl",
           "target_month": results.get("target_month")}
    return out


ICON = {"ok": ":green[✓]", "warn": ":orange[!]", "error": ":red[✕]"}


P_REVIEW, P_PUBLISH = ("Step 1 · Review & adjust", "Step 2 · Publish")

FRIENDLY = {"warehouse_id": "Building ID", "location": "Building", "zone": "Ship-to zone", "product_type": "Product",
            "dept_nbr": "Dept", "dept_name": "Department", "storage": "Storage type", "units": "Units",
            "unit_cost": "Cost / unit", "cost": "Shipping cost", "method": "Placed by", "allocated": "Units placed",
            "capacity": "Capacity", "used_pct": "Space used", "overflow": "Over capacity by",
            "share_pct": "Share of units", "min_pct": "Min target", "max_pct": "Max target"}
TABLE_NAMES = {"allocation_plan": "Allocation plan", "capacity_summary": "Space used by building",
               "warehouse_share": "Share by building", "run_log": "Run summary"}


def page_title(title: str, intro: str):
    st.markdown(f"## {title}")
    st.markdown(f"<p style='color:#5E5E5A;margin:-6px 0 14px;font-size:15.5px'>{intro}</p>", unsafe_allow_html=True)


def show_checks(checks: list, ok_text: str = "Everything looks good."):
    errs = [c for c in checks if c["level"] == "error"]
    warns = [c for c in checks if c["level"] == "warn"]
    oks = [c for c in checks if c["level"] == "ok"]
    for c in errs:
        st.error(c["text"], icon=":material/block:")
    for c in warns:
        st.warning(c["text"], icon=":material/warning:")
    if not errs and not warns:
        st.success(ok_text, icon=":material/check_circle:")
    if oks:
        with st.expander(f"{len(oks)} checks passed"):
            for c in oks:
                st.markdown(f":green[✓] &nbsp; {c['text']}")


def _fmt_cols(df):
    cfg = {}
    for c in df.columns:
        lab = FRIENDLY.get(c, c.replace("_", " ").capitalize())
        if c in ("units", "allocated", "capacity", "overflow"):
            cfg[c] = st.column_config.NumberColumn(lab, format="localized")
        elif c in ("cost",):
            cfg[c] = st.column_config.NumberColumn(lab, format="dollar")
        elif c in ("unit_cost",):
            cfg[c] = st.column_config.NumberColumn(lab, format="$%.2f")
        elif c in ("share_pct", "min_pct", "max_pct"):
            cfg[c] = st.column_config.NumberColumn(lab, format="%.1f%%")
        else:
            cfg[c] = lab
    return cfg


def _excel_bytes(tables: dict) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for k, df in tables.items():
            d = df.drop(columns=[c for c in ("published_at", "adjustments_json") if c in df.columns])
            d.rename(columns=FRIENDLY).to_excel(xw, sheet_name=TABLE_NAMES.get(k, k)[:31], index=False)
    return buf.getvalue()


def page_output():
    page_title("Step 1 · Review & adjust the plan",
               "Check this month's allocation plan and make the changes from the planner input. "
               "Nothing is shared until you publish in step 2.")
    ss.setdefault("month", month_options()[2])
    ensure_demo_gcs(ss["month"])
    with st.expander("Use a different plan file (optional)"):
        c1, c2 = st.columns(2)
        f = c1.file_uploader("Plan file — Excel or CSV", type=["xlsx", "csv", "pkl"])
        capf = c2.file_uploader("Capacity file (optional) — building, storage type, capacity", type=["xlsx", "csv"])
        st.caption("The plan file needs these columns: warehouse, zone, product_type, storage, units. "
                   "Only use .pkl files produced by the allocation pipeline.")
        if f is not None and st.button("Use this file"):
            try:
                o = read_results_pickle(f.getvalue()) if f.name.lower().endswith(".pkl") else {"allocation": read_table_file(f)}
                o["source"] = f.name
                if capf is not None:
                    o["capacity"] = read_table_file(capf)
                ss["alt_out"] = o
                st.rerun()
            except Exception as e:
                st.error(f"This file could not be read: {e}")
        if ss.get("alt_out") and st.button("Go back to the latest plan"):
            ss.pop("alt_out"); ss.pop("adj_sig", None); st.rerun()
    out = ss.get("alt_out") or load_pipeline_output()
    src_label = out.get("source") if ss.get("alt_out") else "latest monthly plan"

    st.markdown("#### Plan details")
    c1, c2, c3 = st.columns([1, 1, 2])
    ss.setdefault("run_id", "RUN-" + datetime.now().strftime("%y%m%d-%H%M"))
    month = c1.selectbox("Plan month", month_options(), key="month")
    run_id = c2.text_input("Run ID", key="run_id", help="A label for this version of the plan.")
    notes = c3.text_input("Notes (optional)", key="notes",
                          placeholder="e.g. Portland & W. Virginia home capacity lowered per planner email")
    st.caption(f"Source: {src_label}")

    checks = validate_output(out)
    if any(c["level"] == "error" for c in checks):
        show_checks(checks)
        return

    adj_out, adjustments = adjust_section(out)
    a = adj_out["allocation"]
    checks = validate_output(adj_out) if len(a) else [{"level": "error", "text": "Nothing left after the exclusions"}]
    tables = None
    if len(a):
        tables = derive_tables(adj_out, {"run_id": run_id, "target_month": "", "env": "", "model_version": "",
                                         "notes": ""}, "", "")
        checks += plan_checks(tables, adj_out["limits"])
    errors = any(c["level"] == "error" for c in checks)

    st.markdown("#### The plan after your changes")
    if tables is not None:
        units = pd.to_numeric(a["units"], errors="coerce")
        cost = pd.to_numeric(a["cost"], errors="coerce") if "cost" in a else None
        base_units = pd.to_numeric(out["allocation"]["units"], errors="coerce").sum()
        k = st.columns(4)
        k[0].metric("Units to place", f"{units.sum():,.0f}",
                    delta=f"{units.sum() - base_units:,.0f} vs original plan" if abs(units.sum() - base_units) > 0.5 else None,
                    delta_color="off", border=True)
        k[1].metric("Shipping cost", f"${cost.sum():,.0f}" if cost is not None else "—", border=True)
        k[2].metric("Buildings", f"{a['warehouse'].nunique()}", border=True)
        k[3].metric("Departments", f"{a['product_type'].map(_dept).nunique()}", border=True)
        t1, t2, t3 = st.tabs(["Share by building", "Space used", "Plan detail"])
        with t1:
            shv = tables["warehouse_share"][["warehouse_id", "location", "units", "share_pct", "min_pct", "max_pct"]].copy()
            shv["in_range"] = np.where((shv["share_pct"] >= shv["min_pct"] - 0.05) &
                                       (shv["share_pct"] <= shv["max_pct"] + 0.05), "✓ Within target", "✕ Outside target")
            st.dataframe(shv, hide_index=True, width="stretch", column_config={**_fmt_cols(shv), "in_range": "Target"})
        with t2:
            cs = tables["capacity_summary"][["warehouse_id", "location", "storage", "allocated", "capacity", "used_pct",
                                             "overflow"]]
            top = max(100.0, float(pd.to_numeric(cs["used_pct"], errors="coerce").fillna(0).max()))
            cfg = _fmt_cols(cs)
            cfg["used_pct"] = st.column_config.ProgressColumn("Space used", min_value=0, max_value=top, format="%.0f%%")
            st.dataframe(cs, hide_index=True, width="stretch", column_config=cfg)
        with t3:
            pdv = tables["allocation_plan"][["warehouse_id", "location", "zone", "dept_nbr", "dept_name", "product_type",
                                             "storage", "units", "unit_cost", "cost", "method"]]
            st.dataframe(pdv, hide_index=True, width="stretch", column_config=_fmt_cols(pdv))

    st.markdown("#### Ready to publish?")
    show_checks(checks, "Everything looks good — you can publish this plan.")
    if st.button("Continue to publish →", type="primary", disabled=errors):
        ss["output"] = adj_out
        ss["meta"] = {"run_id": run_id.strip(), "month_label": month,
                      "target_month": datetime.strptime(month, "%B %Y").date().isoformat(),
                      "env": "prod", "model_version": "alloc_solver_v2", "notes": notes, "adjustments": adjustments}
        ss["_goto"] = P_PUBLISH
        st.rerun()
    if errors:
        st.caption("Fix the items in red above to continue.")


# ---------- adjustments: the 4 questions of the planner input email ----------
#   1. storage types to include / exclude   2. min / max penetration per building
#   3. building capacities (+ pooled caps)  4. departments to exclude
def _dept(pt) -> str:
    return str(pt).split("-")[0]


def _init_adjust(out: dict, sig):
    a = out["allocation"]
    cap = out.get("capacity")
    locs = dict(out.get("locations") or {})
    if "location" in a.columns:
        locs.update({str(w): l for w, l in a[["warehouse", "location"]].dropna().drop_duplicates().values})
    storages = list(dict.fromkeys(
        (list(cap["storage"].astype(str).unique()) if cap is not None else []) + sorted(a["storage"].astype(str).unique())))
    whs = list(dict.fromkeys(list(locs) + sorted(a["warehouse"].astype(str).unique())))
    wide = pd.DataFrame({"warehouse_id": whs, "location": [locs.get(w, "") for w in whs]})
    lim = out.get("limits")
    if lim is not None:
        lm = lim.assign(warehouse=lim["warehouse"].astype(str)).set_index("warehouse")
        wide["min_pct"] = wide["warehouse_id"].map(lm["min_pct"]).astype(float)
        wide["max_pct"] = wide["warehouse_id"].map(lm["max_pct"]).astype(float)
    else:
        wide["min_pct"], wide["max_pct"] = float("nan"), float("nan")
    for k in storages:
        if cap is not None:
            m = cap.assign(warehouse=cap["warehouse"].astype(str), storage=cap["storage"].astype(str))
            m = m[m["storage"] == k].set_index("warehouse")["capacity"]
            wide[k] = wide["warehouse_id"].map(m).astype(float)
        else:
            wide[k] = float("nan")
    pooled = out.get("pooled")
    pooled = (pooled.copy() if pooled is not None else
              pd.DataFrame(columns=["warehouse_id", "storage_types", "pooled_capacity"]))
    pooled["warehouse_id"] = pooled["warehouse_id"].astype(str)
    pooled["pooled_capacity"] = pd.to_numeric(pooled["pooled_capacity"], errors="coerce")
    dm = out.get("departments")
    if dm is None:
        dm = pd.DataFrame({"dept_nbr": sorted(a["product_type"].map(_dept).unique())}).assign(dept_name="", dept_group="")
    dm = dm.astype(str)
    dx = out.get("dept_exclusions") or {"groups": [], "departments": []}
    ss.update({"adj_sig": sig, "storage_list": storages, "added_storage": [], "bld_saved": wide.copy(),
               "bld_orig": wide.copy(), "bld_ver": ss.get("bld_ver", 0) + 1, "inc_storage": storages,
               "pool_saved": pooled.copy(), "pool_orig": pooled.copy(), "pool_ver": ss.get("pool_ver", 0) + 1,
               "dept_master": dm, "excl_groups": [g for g in dx.get("groups", []) if g in set(dm["dept_group"])],
               "excl_groups_orig": list(dx.get("groups", [])),
               "excl_depts": [d for d in dx.get("departments", []) if d in set(dm["dept_nbr"])]})


def _add_storage():
    name = (ss.get("new_storage") or "").strip().upper()
    if not name or not re.match(r"^[A-Z0-9_ -]{1,20}$", name) or name in ss["storage_list"]:
        ss["add_msg"] = "Enter a new, short name (letters, digits, _ or -)."
        return
    ss["storage_list"] = ss["storage_list"] + [name]
    ss["added_storage"] = ss["added_storage"] + [name]
    ss["inc_storage"] = list(ss.get("inc_storage", [])) + [name]
    b = ss["bld_saved"].copy(); b[name] = 0.0
    ss["bld_saved"] = b; ss["bld_ver"] += 1
    ss["new_storage"] = ""; ss["add_msg"] = f"Added {name}. Set its capacity in tab 3 · Building capacity."


def _save_editor(edited, base_key, ver_key, cols=None):
    base = ss[base_key] if cols is None else ss[base_key][cols]
    if not edited.reset_index(drop=True).equals(base.reset_index(drop=True)):
        if cols is None:
            ss[base_key] = edited.reset_index(drop=True).copy()
        else:
            b = ss[base_key].copy()
            for c in cols:
                b[c] = edited[c].values
            ss[base_key] = b
        ss[ver_key] += 1
        st.rerun()


def _dept_label(d):
    m = ss["dept_master"].set_index("dept_nbr")
    if d in m.index and m.loc[d, "dept_name"]:
        return f"D{d} – {m.loc[d, 'dept_name']} ({m.loc[d, 'dept_group']})"
    return f"D{d}"


def adjust_section(out: dict):
    sig = (out.get("source"), len(out["allocation"]), tuple(out["allocation"].columns), DEMO_VERSION)
    if ss.get("adj_sig") != sig:
        _init_adjust(out, sig)
    a = out["allocation"].copy()
    a["warehouse"] = a["warehouse"].astype(str); a["storage"] = a["storage"].astype(str)
    a["units"] = pd.to_numeric(a["units"], errors="coerce")
    a["dept"] = a["product_type"].map(_dept)
    dm = ss["dept_master"]
    a["dept_group"] = a["dept"].map(dict(dm[["dept_nbr", "dept_group"]].values)).fillna("")

    st.markdown("#### Adjust before publishing")
    st.caption("The four questions from the monthly planner input. Change anything here — the plan below updates straight away.")
    t1, t2, t3, t4 = st.tabs(["1 · Storage types", "2 · Min / max penetration", "3 · Building capacity",
                              "4 · Departments"])
    with t1:
        st.caption("Which storage types do we want to include? Untick a type to leave it out of the plan.")
        inc = st.multiselect("Storage types to include", ss["storage_list"], key="inc_storage")
        c1, c2 = st.columns([2, 1])
        c1.text_input("New storage type", key="new_storage", placeholder="e.g. BULK")
        c2.write(""); c2.write("")
        c2.button("Add storage type", on_click=_add_storage, width="stretch")
        if ss.get("add_msg"):
            st.caption(ss.pop("add_msg"))
        st.dataframe(a.groupby("storage", as_index=False).agg(rows=("units", "size"), units=("units", "sum"))
                     .assign(included=lambda d: d["storage"].isin(inc)), hide_index=True, width="stretch",
                     column_config={"storage": "Storage type", "rows": "Plan lines", "included": "Included",
                                    "units": st.column_config.NumberColumn("Units", format="localized")})
    with t2:
        st.caption("What should the min / max penetration targets be? Each building’s share of all units.")
        cols = ["warehouse_id", "location", "min_pct", "max_pct"]
        ed = st.data_editor(ss["bld_saved"][cols], key=f"pen_{ss['bld_ver']}", hide_index=True, width="stretch",
                            disabled=["warehouse_id"], column_config={
                                "warehouse_id": "ID", "location": "Location",
                                "min_pct": st.column_config.NumberColumn("min_penetration %", min_value=0.0,
                                                                         max_value=100.0, step=0.5, format="%.1f%%"),
                                "max_pct": st.column_config.NumberColumn("max_penetration %", min_value=0.0,
                                                                         max_value=100.0, step=0.5, format="%.1f%%")})
        _save_editor(ed, "bld_saved", "bld_ver", cols)
        b = ss["bld_saved"]
        smin, smax = b["min_pct"].fillna(0).sum(), b["max_pct"].fillna(0).sum()
        st.caption(f"Minimums add up to {smin:.1f}% (must be 100% or less) · maximums add up to {smax:.1f}% (must be 100% or more)")
    with t3:
        st.caption("Any changes to the building capacities? Units per storage type. Leave a cell empty (None) if the building "
                   "has no space for that type.")
        cols = ["warehouse_id", "location"] + [k for k in ss["storage_list"] if k in ss["bld_saved"].columns]
        ed = st.data_editor(ss["bld_saved"][cols], key=f"cap_{ss['bld_ver']}", hide_index=True, width="stretch",
                            disabled=["warehouse_id", "location"],
                            column_config={"warehouse_id": "ID", "location": "Location",
                                           **{k: st.column_config.NumberColumn(k, min_value=0, step=1000, format="localized")
                                              for k in ss["storage_list"]}})
        _save_editor(ed, "bld_saved", "bld_ver", cols)
        st.markdown("**Shared capacity** — storage types in a building that share one limit (e.g. Bailey Road FLA + HFG).")
        ped = st.data_editor(ss["pool_saved"], key=f"pool_{ss['pool_ver']}", hide_index=True, width="stretch",
                             num_rows="dynamic", column_config={
                                 "warehouse_id": st.column_config.SelectboxColumn(
                                     "ID", options=list(ss["bld_saved"]["warehouse_id"]), required=True),
                                 "storage_types": st.column_config.TextColumn("Storage types sharing the limit (e.g. FLA+HFG)", required=True),
                                 "pooled_capacity": st.column_config.NumberColumn("Shared capacity", min_value=0,
                                                                                  step=1000, format="localized")})
        _save_editor(ped, "pool_saved", "pool_ver")
        if st.button("Undo my changes to targets and capacity"):
            keep = ss["bld_orig"].copy()
            for k in ss["added_storage"]:
                keep[k] = 0.0
            ss["bld_saved"] = keep; ss["bld_ver"] += 1
            ss["pool_saved"] = ss["pool_orig"].copy(); ss["pool_ver"] += 1
            st.rerun()
    with t4:
        st.caption("Which departments should be excluded? Leave out whole groups (e.g. Big Ticket, Food) or single departments.")
        groups = sorted(g for g in dm["dept_group"].unique() if g)
        excl_g = st.multiselect("Department groups to exclude", groups, key="excl_groups")
        depts = sorted(a["dept"].unique(), key=lambda x: int(x) if x.isdigit() else 0)
        excl = st.multiselect("Departments to exclude", depts, key="excl_depts", format_func=_dept_label)
        tbl = a.groupby(["dept", "dept_group"], as_index=False).agg(product_types=("product_type", "nunique"),
                                                                    units=("units", "sum"))
        tbl["dept_name"] = tbl["dept"].map(dict(dm[["dept_nbr", "dept_name"]].values)).fillna("")
        tbl["excluded"] = tbl["dept"].isin(excl) | tbl["dept_group"].isin(excl_g)
        st.dataframe(tbl[["dept", "dept_name", "dept_group", "product_types", "units", "excluded"]],
                     hide_index=True, width="stretch", column_config={
                         "dept": "Dept", "dept_name": "Department", "dept_group": "Group", "product_types": "Products", "excluded": "Excluded",
                         "units": st.column_config.NumberColumn("Units", format="localized")})

    # ---- apply
    keep = a["storage"].isin(inc) & ~a["dept"].isin(excl) & ~a["dept_group"].isin(excl_g)
    a2 = out["allocation"][keep.values].reset_index(drop=True)
    a2["warehouse"] = a2["warehouse"].astype(str)
    a2["storage"] = a2["storage"].astype(str)
    b = ss["bld_saved"]
    stor_cols = [k for k in ss["storage_list"] if k in b.columns]
    cap_long = b[["warehouse_id"] + stor_cols].melt(id_vars=["warehouse_id"], var_name="storage", value_name="capacity") \
        .rename(columns={"warehouse_id": "warehouse"})
    cap_long = cap_long[cap_long["storage"].isin(inc)].dropna(subset=["capacity"])[["warehouse", "storage", "capacity"]]
    lim = b[["warehouse_id", "min_pct", "max_pct"]].rename(columns={"warehouse_id": "warehouse"})
    locs = {str(w): (l or "") for w, l in b[["warehouse_id", "location"]].values}
    pooled = ss["pool_saved"].dropna(subset=["warehouse_id", "storage_types"])

    orig = ss["bld_orig"].set_index("warehouse_id")

    def _num(v):
        return float(v) if pd.notna(v) else None
    cap_changes, pen_changes = [], []
    for _, r in b.iterrows():
        w = r["warehouse_id"]
        for k in stor_cols:
            old = _num(orig.at[w, k]) if w in orig.index and k in orig.columns else None
            new = _num(r[k])
            if old != new and not (old is None and not new):
                cap_changes.append({"warehouse_id": w, "location": r["location"], "storage": k, "from": old, "to": new})
        for c in ("min_pct", "max_pct"):
            old = _num(orig.at[w, c]) if w in orig.index else None
            if old != _num(r[c]):
                pen_changes.append({"warehouse_id": w, "location": r["location"], "field": c, "from": old, "to": _num(r[c])})
    loc_changes = {w: l for w, l in locs.items() if w in orig.index and l != (orig.at[w, "location"] or "")}
    po = ss["pool_orig"]
    pool_changed = not pooled.reset_index(drop=True).equals(po.reset_index(drop=True))
    adjustments = {
        "excluded_storage": [k for k in ss["storage_list"] if k not in inc], "added_storage": ss["added_storage"],
        "penetration_changes": pen_changes, "capacity_changes": cap_changes,
        "pooled_capacity": pooled.to_dict(orient="records") if pool_changed else [],
        "excluded_dept_groups": excl_g if sorted(excl_g) != sorted(ss.get("excl_groups_orig", [])) else [],
        "excluded_departments": [_dept_label(d) for d in excl],
        "location_changes": loc_changes}
    shown = {k: v for k, v in adjustments.items() if v}
    if shown:
        st.info("Your changes: " + "; ".join(f"{k.replace('_', ' ')} ({len(v)})" for k, v in shown.items())
                + ". They are saved with the plan when you publish.", icon=":material/edit_note:")
    adj = dict(out, allocation=a2, capacity=cap_long if len(cap_long) else None, locations=locs, limits=lim,
               pooled=pooled, departments=dm)
    return adj, adjustments


def plan_checks(tables: dict, limits: pd.DataFrame) -> list[dict]:
    """Business checks on the adjusted plan: penetration targets, capacity, pooled caps."""
    out = []
    l = limits
    bad = l[(l["min_pct"] > l["max_pct"])]
    out.append({"level": "error" if len(bad) else "ok", "text": "Each building’s minimum target is below its maximum"
                + (f" — fails for {', '.join(bad['warehouse'])}" if len(bad) else "")})
    smin, smax = l["min_pct"].fillna(0).sum(), l["max_pct"].fillna(0).sum()
    out.append({"level": "ok" if smin <= 100 else "error", "text": f"Minimum targets add up to 100% or less ({smin:.1f}%)"})
    out.append({"level": "ok" if smax >= 100 else "error", "text": f"Maximum targets add up to 100% or more ({smax:.1f}%)"})
    sh = tables["warehouse_share"]
    off = sh[(sh["share_pct"] < sh["min_pct"] - 0.05) | (sh["share_pct"] > sh["max_pct"] + 0.05)]
    out.append({"level": "warn" if len(off) else "ok",
                "text": "Every building is within its min / max target" if not len(off) else
                "Outside the min / max target: " + ", ".join(f"{r.location or r.warehouse_id} {r.share_pct:.1f}% "
                                                       f"(target {r.min_pct:.0f}–{r.max_pct:.0f}%)" for r in off.itertuples())})
    cs = tables["capacity_summary"]
    over = cs[pd.to_numeric(cs["overflow"], errors="coerce").fillna(0) > 0]
    out.append({"level": "warn" if len(over) else "ok",
                "text": "No building over capacity" if not len(over) else
                "Over capacity: " + ", ".join(f"{r.location or r.warehouse_id} {r.storage} by {r.overflow:,.0f}"
                                              for r in over.itertuples())})
    nocap = cs[cs["capacity"].isna() & (pd.to_numeric(cs["allocated"], errors="coerce").fillna(0) > 0)]
    if len(nocap):
        out.append({"level": "warn", "text": "Units placed where no capacity is set: " + ", ".join(
            f"{r.location or r.warehouse_id} {r.storage}" for r in nocap.itertuples())})
    return out



def page_publish():
    live = bq_live()
    page_title("Step 2 · Publish the plan",
               "Share the plan so reports and the warehouse teams can use it. You can download a copy at any time.")
    if not live:
        st.caption(":gray-badge[Demo mode] The plan is saved inside this app only — nothing is sent anywhere.")
    out, meta = ss.get("output"), ss.get("meta")
    if not out:
        st.info("Review the plan first.")
        if st.button("Go to Step 1 · Review & adjust", type="primary"):
            ss["_goto"] = P_REVIEW; st.rerun()
        return

    cfg = dict(_secrets().get("bigquery", {})) if live else {}
    ss.setdefault("pub_project", cfg.get("project", "demo-allocation-project"))
    ss.setdefault("pub_dataset", cfg.get("dataset", "com_allocation"))
    ss.setdefault("pub_location", cfg.get("location", "US"))
    project, dataset, location = ss["pub_project"], ss["pub_dataset"], ss["pub_location"]
    names = {k: k for k in BQ_TABLES}

    published_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tables = derive_tables(out, meta, ss["user"]["name"], published_at)
    n_changes = sum(len(v) for v in meta.get("adjustments", {}).values() if v)
    k = st.columns(4)
    k[0].metric("Plan month", meta["month_label"], border=True)
    k[1].metric("Units", f"{tables['allocation_plan']['units'].sum():,.0f}", border=True)
    k[2].metric("Buildings", f"{tables['warehouse_share'].shape[0]}", border=True)
    k[3].metric("Your changes", f"{n_changes}", border=True)
    st.caption(f"Run {meta['run_id']}" + (f" · {meta['notes']}" if meta.get("notes") else ""))

    st.markdown("#### What will be published")
    tabs = st.tabs([TABLE_NAMES[k] for k in names])
    show = {"allocation_plan": ["location", "zone", "dept_name", "product_type", "storage", "units", "cost"],
            "capacity_summary": ["location", "storage", "allocated", "capacity", "overflow"],
            "warehouse_share": ["warehouse_id", "location", "units", "share_pct", "min_pct", "max_pct"],
            "run_log": ["run_id", "target_month", "total_units", "total_cost", "notes", "published_by"]}
    for tab, k in zip(tabs, names):
        with tab:
            d = tables[k][show[k]]
            st.caption(BQ_TABLES[k]["desc"])
            st.dataframe(d.head(300), hide_index=True, width="stretch", column_config=_fmt_cols(d))

    st.markdown("#### If this month was published before")
    mode_l = st.radio("Publishing option", ["Replace the earlier version for this month (recommended)",
                                            "Keep the earlier version and add this one"],
                      label_visibility="collapsed")
    mode = "replace" if mode_l.startswith("Replace") else "append"

    with st.expander("Destination settings (for admins)"):
        a1, a2, a3 = st.columns(3)
        a1.text_input("Project", key="pub_project")
        a2.text_input("Dataset", key="pub_dataset")
        a3.text_input("Location", key="pub_location")
        st.caption("Tables: " + ", ".join(f"{TABLE_NAMES[k]} → `{k}`" for k in names))

    ok_ids = bool(_ID_PROJECT.match(project or "")) and bool(_ID_NAME.match(dataset or ""))
    checks = [
        {"level": "ok" if ok_ids else "error",
         "text": "Destination is set" if ok_ids else "Destination settings are not valid — ask an admin to check them"},
        {"level": "ok" if meta["run_id"] else "error", "text": "Run ID is set" if meta["run_id"] else "Run ID is empty"},
        {"level": "ok" if abs(tables["allocation_plan"]["units"].sum() - tables["run_log"]["total_units"][0]) < 1
         else "error", "text": "Totals add up"},
    ]
    plan_cols = ["warehouse_id", "zone", "product_type", "storage", "units"]
    plan_hash = hashlib.sha256(tables["allocation_plan"][plan_cols].sort_values(plan_cols[:4]).round(2)
                               .to_csv(index=False).encode()).hexdigest()[:16]
    prev = [p for p in read_pub_log() if p["target_month"] == meta["target_month"] and p["target"] == f"{project}.{dataset}"]
    dup = next((p for p in prev if p.get("plan_hash") == plan_hash), None)
    lp = ss.get("last_pub")
    just_done = bool(lp and lp.get("hash") == plan_hash)
    if just_done:
        pass
    elif dup:
        allow_dup = st.checkbox("Publish this same plan again anyway", key="allow_dup")
        checks.append({"level": "warn" if allow_dup else "error",
                       "text": f"This exact plan was already published for {meta['month_label']} "
                               f"by {dup['by']} on {dup['at']} (run {dup['run_id']})."})
    else:
        checks.append({"level": "ok", "text": "Not a duplicate of a plan already published"})
    if prev and not dup:
        checks.append({"level": "warn", "text": f"{meta['month_label']} was published before — "
                       + ("the earlier version will be replaced." if mode == "replace"
                          else "both versions will be kept.")})
    if just_done:
        st.success(f"Published — the {lp['month']} plan ({lp['units']:,.0f} units) is now available for reports.",
                   icon=":material/check_circle:")
        blocked = True
    else:
        show_checks(checks, "Ready to publish.")
        blocked = any(c["level"] == "error" for c in checks)

    b1, b2, _ = st.columns([1, 1, 2])
    if b1.button("Publish plan", type="primary", disabled=blocked, width="stretch"):
        try:
            with st.spinner("Publishing…"):
                rows = (bq_publish(bq_client(project, location), tables, project, dataset, names, meta["target_month"], mode)
                        if live else demo_publish(tables, dataset, names, meta["target_month"], mode))
            add_pub_log({"at": published_at, "by": ss["user"]["name"], "run_id": meta["run_id"],
                         "target_month": meta["target_month"], "month_label": meta["month_label"],
                         "target": f"{project}.{dataset}", "mode": mode, "rows": rows,
                         "units": float(tables["allocation_plan"]["units"].sum()), "changes": n_changes,
                         "backend": "bigquery" if live else "demo", "plan_hash": plan_hash})
            ss["last_pub"] = {"month": meta["month_label"], "units": float(tables["allocation_plan"]["units"].sum()),
                              "hash": plan_hash}
            st.rerun()
        except ModuleNotFoundError:
            st.error("Publishing isn't set up yet — ask an admin to connect the app.")
        except Exception as e:
            st.error(f"Publishing failed. Please try again or contact an admin. ({type(e).__name__})")
    b2.download_button("Download as Excel", _excel_bytes(tables),
                       file_name=f"allocation_plan_{meta['target_month']}.xlsx", width="stretch",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    log = read_pub_log()
    if log:
        st.markdown("#### Publishing history")
        st.dataframe(pd.DataFrame([{
            "Published": p["at"], "By": p["by"], "Plan month": p.get("month_label", p["target_month"]),
            "Run": p["run_id"], "Units": p.get("units"), "Changes made": p.get("changes"),
            "Earlier version": "Replaced" if p["mode"] == "replace" else "Kept"} for p in reversed(log)]),
            hide_index=True, width="stretch",
            column_config={"Units": st.column_config.NumberColumn(format="localized")})



PAGES = {P_REVIEW: page_output, P_PUBLISH: page_publish}


def main():
    if "PORTAL" in globals():  # signed in through the portal (streamlit_app.py)
        ss["user"] = PORTAL["user"]
    if "user" not in ss:
        if ss.get("view") == "login":
            login_page()
        else:
            landing_page()
        return
    if "_goto" in ss:
        ss["page"] = ss.pop("_goto")
    if ss.get("page") not in PAGES:
        ss["page"] = P_REVIEW
    with st.sidebar:
        if "PORTAL" in globals():
            PORTAL["sidebar"]()
        else:
            st.markdown("### Allocation Planner")
            st.markdown(f"**{ss['user']['name']}**  \nPlanner")
        st.radio("Navigate", list(PAGES), key="page", label_visibility="collapsed")
        with st.expander("How to use"):
            st.markdown("1. **Review & adjust** — check the plan and enter this month's changes: storage types, "
                        "min / max targets, building capacity, departments.\n"
                        "2. **Publish** — share the plan for reports, or download it as Excel.")
        if "PORTAL" not in globals():
            st.divider()
            if st.button("Log out", width="stretch"):
                for k in list(ss.keys()):
                    del ss[k]
                st.rerun()
    PAGES[ss["page"]]()


main()
