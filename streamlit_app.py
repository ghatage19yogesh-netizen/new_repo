"""
Supply Chain Tools — one Streamlit app with ONE sign-in page for two tools:

  • Allocation Planner       (apps/allocation_planner.py)  — monthly COM inventory allocation
  • Dynamic Fulfillment      (apps/dynamic_fulfillment.py) — product exclusion tool

Run:
    pip install -r requirements.txt
    streamlit run streamlit_app.py

Everyone signs in on the same page. Each account lists the tools it may use, and after sign-in
the user only sees (and can only open) those tools.
Add a user (prints a block to paste into .streamlit/secrets.toml or Streamlit Cloud Secrets):
    python streamlit_app.py adduser <username> "<Full Name>" <password> alloc,df
    python streamlit_app.py adduser <username> "<Full Name>" <password> alloc
    python streamlit_app.py adduser <username> "<Full Name>" <password> df
"""
from __future__ import annotations

import hashlib
import hmac
import json
import runpy
import secrets as pysecrets
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent


# ======================================================================================
# Passwords (same PBKDF2 scheme the Allocation Planner already used)
# ======================================================================================
def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


def make_user(name: str, password: str) -> dict:
    salt = pysecrets.token_hex(16)
    return {"name": name, "salt": salt, "password_hash": hash_password(password, salt)}


TOOL_KEYS = ("alloc", "df")
SECTION = {"alloc": "alloc_users", "df": "df_users"}  # older per-tool sections, still accepted

if __name__ == "__main__" and len(sys.argv) >= 2 and sys.argv[1] == "adduser":
    _tools = [t.strip() for t in (sys.argv[5] if len(sys.argv) == 6 else "").split(",") if t.strip()]
    if len(sys.argv) != 6 or not _tools or any(t not in TOOL_KEYS for t in _tools):
        print('usage: python streamlit_app.py adduser <username> "<Full Name>" <password> <alloc|df|alloc,df>')
        sys.exit(1)
    _u, _n, _p = sys.argv[2:5]
    print(f"[portal_users.{_u.strip().lower()}]")
    for _k, _v in {**make_user(_n, _p), "tools": _tools}.items():
        print(f"{_k} = {json.dumps(_v)}")
    sys.exit(0)

import streamlit as st  # noqa: E402

# ======================================================================================
# The two tools
# ======================================================================================
_DEMO_SALT = "8f1d2c3b4a5968778695a4b3c2d1e0f1"
TOOLS = {
    "alloc": {
        "name": "Allocation Planner",
        "kicker": "Monthly COM inventory allocation",
        "icon": ":material/inventory_2:",
        "emoji": "📦",
        "who": "For planners",
        "summary": "Review the monthly allocation plan, apply the planner input and publish it for reports.",
        "can": ["Check where inventory is going, by building and department",
                "Change storage types, min / max targets, building capacity and departments",
                "Publish the plan or download it as Excel"],
        "file": "allocation_planner.py",
        "layout": "wide",
    },
    "df": {
        "name": "Dynamic Fulfillment",
        "kicker": "Product exclusion tool",
        "icon": ":material/local_shipping:",
        "emoji": "🚚",
        "who": "For the fulfillment team",
        "summary": "Choose which products each fulfillment process should skip, or bring them back.",
        "can": ["Exclude products from CS Margin, Sellout Risk, Replenishment or VDF Margin",
                "Bring excluded products back",
                "Check which processes a product is excluded from"],
        "file": "dynamic_fulfillment.py",
        "layout": "centered",
    },
}


def find_tool_file(filename: str) -> Path | None:
    """Finds a tool's file wherever it was uploaded: apps/<file>, next to streamlit_app.py, or any subfolder."""
    for p in (HERE / "apps" / filename, HERE / filename):
        if p.is_file():
            return p
    for p in sorted(HERE.rglob(filename)):
        if p.is_file() and not any(part.startswith(".") or part in ("venv", "site-packages") for part in p.parts):
            return p
    return None


def _secrets():
    try:
        return st.secrets
    except Exception:
        return {}


# Demo accounts — used only until real users are added to Secrets
DEMO_ACCOUNTS = [  # username, full name, password, tools
    ("demo", "Demo User", "demo123", ["alloc", "df"]),
    ("yogesh", "Yogesh", "planner123", ["alloc"]),
    ("df.user", "DF User", "df@123", ["df"]),
]


def load_users() -> tuple[dict, bool]:
    """One user list for the whole app: {username: {name, salt, password_hash, tools}}, is_demo.

    Reads [portal_users.*] (preferred). Older blocks still work: [alloc_users.*] / [users.*] give
    Allocation Planner access, [df_users.*] gives Dynamic Fulfillment access.
    """
    sec = _secrets()
    users: dict = {}

    def add(section, tools_default, keep=lambda v: True):
        try:
            block = sec[section]
        except Exception:
            return
        for name, v in block.items():
            v = dict(v)
            if not keep(v):
                continue
            tools = list(v.get("tools") or tools_default)
            u = users.setdefault(name.lower(), {**v, "tools": [], "creds": []})
            u["creds"].append((v["salt"], v["password_hash"], tools))
            u.setdefault("name", v.get("name", name))

    add("portal_users", [])
    add(SECTION["alloc"], ["alloc"])
    add("users", ["alloc"], keep=lambda v: v.get("role", "planner") == "planner")
    add(SECTION["df"], ["df"])
    if users:
        return users, False
    return {u: {"name": n, "creds": [(_DEMO_SALT, hash_password(p, _DEMO_SALT), t)]}
            for u, n, p, t in DEMO_ACCOUNTS}, True


def check_login(username: str, password: str):
    """Returns {"username", "name", "tools"} or None. Tools = every tool whose stored password matches."""
    users, _ = load_users()
    key = (username or "").strip().lower()
    u = users.get(key)
    if not u:
        hash_password(password or "", "00" * 16)  # same timing whether or not the user exists
        return None
    tools: list = []
    hashes: dict = {}
    for salt, stored, t in u["creds"]:
        h = hashes.get(salt) or hashes.setdefault(salt, hash_password(password or "", salt))
        if hmac.compare_digest(h, stored):
            tools += [x for x in t if x in TOOLS and x not in tools]
    if not tools:
        return None
    return {"username": key, "name": u.get("name", key), "tools": tools}


# ======================================================================================
# Navigation & sign-in state (all portal keys start with portal_)
# ======================================================================================
ss = st.session_state
ss.setdefault("portal_route", "home")   # home | app:<tool>
ss.setdefault("portal_user", None)      # {"username", "name", "tools"} after sign-in


def go(route: str):
    ss["portal_route"] = route


def sign_out():
    """Signs out of the whole app and clears every tool's data."""
    for k in list(ss.keys()):
        del ss[k]


user = ss["portal_user"]
kind, _, tool = ss["portal_route"].partition(":")
if user is None:
    kind, tool = "login", ""
elif kind == "app" and tool not in user["tools"]:  # unknown tool or no access
    kind, tool = "home", ""
    ss["portal_route"] = "home"

if kind == "app":
    st.set_page_config(page_title=TOOLS[tool]["name"], page_icon=TOOLS[tool]["icon"], layout=TOOLS[tool]["layout"])
elif kind == "login":
    st.set_page_config(page_title="Sign in · Supply Chain Tools", page_icon=":material/hub:", layout="centered")
else:
    st.set_page_config(page_title="Supply Chain Tools", page_icon=":material/hub:", layout="wide")

# ======================================================================================
# Styling
# ======================================================================================
CSS = """
<style>
.pt-eyebrow{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#5E5E5A;font-weight:600;margin:0 0 12px}
.pt-h1{font-size:clamp(32px,4.6vw,50px);line-height:1.06;font-weight:700;letter-spacing:-.02em;margin:0 0 14px;color:#1C1C1A}
.pt-h1 em{font-style:normal;color:#C26A1B}
.pt-lead{font-size:18px;line-height:1.55;color:#3D3D3A;max-width:680px;margin:0 0 6px}
.pt-card{background:#fff;border:1.5px solid #1C1C1A;border-radius:10px;padding:24px 24px 14px;height:100%}
.pt-top{display:flex;align-items:center;gap:14px;margin-bottom:10px}
.pt-ico{width:52px;height:52px;border-radius:12px;background:#F4F3EF;border:1.5px solid #C9C8C2;display:flex;
        align-items:center;justify-content:center;font-size:26px}
.pt-k{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#5E5E5A;font-weight:600}
.pt-name{font-size:22px;font-weight:700;color:#1C1C1A;line-height:1.2}
.pt-who{display:inline-block;font-size:12px;font-weight:600;color:#C26A1B;border:1px solid #E7C29E;background:#FBF1E7;
        border-radius:999px;padding:2px 10px;margin:2px 0 10px}
.pt-sum{color:#3D3D3A;font-size:15px;line-height:1.5;margin:0 0 10px}
.pt-card ul{margin:0 0 6px;padding-left:18px;color:#3D3D3A;font-size:14.5px;line-height:1.55}
.pt-in{font-size:13px;color:#1f7a4d;font-weight:600;margin-top:6px}
.pt-out{font-size:13px;color:#8a8a84;margin-top:6px}
.pt-foot{color:#5E5E5A;font-size:13px;border-top:1px solid #D9D8D2;padding-top:14px;margin-top:22px}
.pt-lock{font-size:13px;color:#8a8a84;margin-top:6px}
.pt-card.off{border-color:#C9C8C2;background:#FAFAF8}
.pt-card.off .pt-name,.pt-card.off .pt-sum,.pt-card.off ul{color:#8a8a84}
.pt-mini{display:flex;gap:10px;margin:6px 0 4px}
.pt-mini div{flex:1;border:1.5px solid #C9C8C2;border-radius:10px;padding:10px 12px;background:#fff;font-size:13.5px;color:#3D3D3A}
.pt-mini b{display:block;color:#1C1C1A;font-size:14.5px}
.pt-login-ico{width:60px;height:60px;border-radius:14px;background:#F4F3EF;border:1.5px solid #C9C8C2;display:flex;
        align-items:center;justify-content:center;font-size:30px;margin:28px 0 10px}
</style>
"""


# ======================================================================================
# Pages
# ======================================================================================
def login_page():
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown('<div class="pt-login-ico">🧭</div>', unsafe_allow_html=True)
    st.markdown("## Supply Chain Tools")
    st.caption("Sign in once to use the tools you have access to.")
    st.markdown(
        '<div class="pt-mini">' + "".join(
            f'<div><b>{t["emoji"]} {t["name"]}</b>{t["kicker"]}</div>' for t in TOOLS.values()) + "</div>",
        unsafe_allow_html=True)
    with st.form("portal_login", border=True):
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        ok = st.form_submit_button("Sign in", type="primary", width="stretch")
    if ok:
        found = check_login(u, p)
        if found:
            ss["portal_user"] = found
            # only one tool on the account -> open it straight away
            ss["portal_route"] = f"app:{found['tools'][0]}" if len(found["tools"]) == 1 else "home"
            st.rerun()
        st.error("Username or password is incorrect.")
    _, demo = load_users()
    if demo:
        with st.expander("Demo accounts"):
            for du, _, dp, dt in DEMO_ACCOUNTS:
                st.markdown(f"`{du}` · `{dp}` — " + " + ".join(TOOLS[x]["name"] for x in dt))
    st.caption("Need access? Ask your team lead — access is given per tool.")


def landing():
    st.markdown(CSS, unsafe_allow_html=True)
    top_l, top_r = st.columns([5, 1])
    top_l.caption(f"Signed in as **{user['name']}**")
    top_r.button("Sign out", key="portal_out_home", width="stretch", on_click=sign_out)
    st.markdown(
        '<p class="pt-eyebrow">Supply chain tools</p>'
        '<h1 class="pt-h1">Plan inventory and control fulfillment — <em>in one place</em>.</h1>'
        f'<p class="pt-lead">Welcome, {user["name"]}. Pick a tool to continue.</p>', unsafe_allow_html=True)
    st.write("")
    cols = st.columns(2, gap="large")
    for col, (key, t) in zip(cols, TOOLS.items()):
        allowed = key in user["tools"]
        with col:
            status = ('<div class="pt-in">● You have access</div>' if allowed
                      else '<div class="pt-lock">🔒 No access — ask your team lead</div>')
            st.markdown(
                f'<div class="pt-card{"" if allowed else " off"}"><div class="pt-top"><div class="pt-ico">{t["emoji"]}</div>'
                f'<div><div class="pt-k">{t["kicker"]}</div><div class="pt-name">{t["name"]}</div></div></div>'
                f'<span class="pt-who">{t["who"]}</span><p class="pt-sum">{t["summary"]}</p>'
                f'<ul>{"".join(f"<li>{c}</li>" for c in t["can"])}</ul>{status}</div>',
                unsafe_allow_html=True)
            st.button(f"Open {t['name']} →", key=f"portal_open_{key}", type="primary", width="stretch",
                      disabled=not allowed, on_click=go, args=(f"app:{key}",))
    st.markdown('<p class="pt-foot">Demo version: sample data only, nothing is sent outside the app.</p>',
                unsafe_allow_html=True)


def run_tool(key: str):
    t = TOOLS[key]

    def sidebar():
        st.markdown(f"**{t['emoji']} {t['name']}**")
        st.caption(f"Signed in as {user['name']}")
        c1, c2 = st.columns(2)
        c1.button("All tools", icon=":material/home:", key=f"portal_home_{key}", width="stretch",
                  on_click=go, args=("home",))
        c2.button("Sign out", key=f"portal_signout_{key}", width="stretch", on_click=sign_out)
        st.divider()

    path = find_tool_file(t["file"])
    if path is None:
        found = sorted(str(f.relative_to(HERE)) for f in HERE.rglob("*.py") if ".venv" not in f.parts)
        with st.sidebar:
            sidebar()
        st.error(f"**{t['name']}** can't start: `{t['file']}` is not in the deployed app.\n\n"
                 f"Upload `{t['file']}` to the GitHub repository (in the `apps` folder or next to "
                 f"`streamlit_app.py`), then reboot the app.")
        st.caption("Python files the app can see: " + ", ".join(f"`{f}`" for f in found))
        return
    try:
        runpy.run_path(str(path), run_name=f"portal_{key}",
                       init_globals={"PORTAL": {"user": {"username": user["username"], "name": user["name"]},
                                             "sidebar": sidebar}})
    except Exception as e:  # Streamlit's rerun/stop signals are BaseException, so they pass through
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)  # also in the Streamlit Cloud logs
        st.error(f"{t['name']} hit a problem: **{type(e).__name__}: {e}**")
        c1, c2 = st.columns(2)
        c1.button("Try again", type="primary", key=f"portal_retry_{key}")
        c2.button("Back to all tools", key=f"portal_err_home_{key}", on_click=go, args=("home",))
        with st.expander("Technical details (share with support)"):
            st.code(tb, language=None)


if kind == "login":
    login_page()
elif kind == "app":
    run_tool(tool)
else:
    landing()
