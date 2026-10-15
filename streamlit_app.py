"""
Supply Chain Tools — one Streamlit app with a shared landing page and a separate sign-in for:

  • Allocation Planner       (apps/allocation_planner.py)  — monthly COM inventory allocation
  • Dynamic Fulfillment      (apps/dynamic_fulfillment.py) — product exclusion tool

Run:
    pip install -r requirements.txt
    streamlit run streamlit_app.py

Each tool has its own user list. Signing in to one tool does not give access to the other.
Add a user (prints a block to paste into .streamlit/secrets.toml or Streamlit Cloud Secrets):
    python streamlit_app.py adduser alloc <username> "<Full Name>" <password>
    python streamlit_app.py adduser df    <username> "<Full Name>" <password>
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


SECTION = {"alloc": "alloc_users", "df": "df_users"}

if __name__ == "__main__" and len(sys.argv) >= 2 and sys.argv[1] == "adduser":
    if len(sys.argv) != 6 or sys.argv[2] not in SECTION:
        print('usage: python streamlit_app.py adduser <alloc|df> <username> "<Full Name>" <password>')
        sys.exit(1)
    _app, _u, _n, _p = sys.argv[2:]
    print(f"[{SECTION[_app]}.{_u.strip().lower()}]")
    for _k, _v in make_user(_n, _p).items():
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
        "file": HERE / "apps" / "allocation_planner.py",
        "layout": "wide",
        "demo": ("yogesh", "Yogesh", "planner123"),
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
        "file": HERE / "apps" / "dynamic_fulfillment.py",
        "layout": "centered",
        "demo": ("df.user", "DF User", "df@123"),
    },
}


def _secrets():
    try:
        return st.secrets
    except Exception:
        return {}


def load_users(tool: str) -> tuple[dict, bool]:
    """Returns ({username: {name, salt, password_hash}}, is_demo)."""
    users = {}
    try:
        users = {k.lower(): dict(v) for k, v in _secrets()[SECTION[tool]].items()}
    except Exception:
        pass
    if tool == "alloc" and not users:  # keep the Allocation Planner's original [users.*] block working
        try:
            users = {k.lower(): dict(v) for k, v in _secrets()["users"].items()
                     if dict(v).get("role", "planner") == "planner"}
        except Exception:
            pass
    if users:
        return users, False
    u, n, p = TOOLS[tool]["demo"]
    return {u: {"name": n, "salt": _DEMO_SALT, "password_hash": hash_password(p, _DEMO_SALT)}}, True


def check_login(tool: str, username: str, password: str):
    users, _ = load_users(tool)
    key = (username or "").strip().lower()
    u = users.get(key)
    if not u:
        hash_password(password or "", "00" * 16)  # same timing whether or not the user exists
        return None
    if hmac.compare_digest(hash_password(password or "", u["salt"]), u["password_hash"]):
        return {"username": key, "name": u.get("name", key)}
    return None


# ======================================================================================
# Navigation & sign-in state (all portal keys start with portal_)
# ======================================================================================
ss = st.session_state
ss.setdefault("portal_route", "home")   # home | login:<tool> | app:<tool>
ss.setdefault("portal_auth", {})        # {tool: {"username", "name"}}


def go(route: str):
    ss["portal_route"] = route


def sign_out(tool: str):
    """Signs out of ONE tool and clears only that tool's data; the other tool stays signed in."""
    ss["portal_auth"].pop(tool, None)
    for k in list(ss.keys()):
        if k.startswith("portal_"):
            continue
        is_df = k.startswith("df_")
        if (tool == "df" and is_df) or (tool == "alloc" and not is_df):
            del ss[k]
    ss["portal_route"] = "home"


route = ss["portal_route"]
kind, _, tool = route.partition(":")
if kind in ("login", "app") and tool not in TOOLS:
    kind, tool = "home", ""
if kind == "app" and tool not in ss["portal_auth"]:
    kind = "login"
    ss["portal_route"] = f"login:{tool}"

if kind == "app":
    st.set_page_config(page_title=TOOLS[tool]["name"], page_icon=TOOLS[tool]["icon"], layout=TOOLS[tool]["layout"])
elif kind == "login":
    st.set_page_config(page_title=f"Sign in · {TOOLS[tool]['name']}", page_icon=TOOLS[tool]["icon"], layout="centered")
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
.pt-login-ico{width:60px;height:60px;border-radius:14px;background:#F4F3EF;border:1.5px solid #C9C8C2;display:flex;
        align-items:center;justify-content:center;font-size:30px;margin:28px 0 10px}
</style>
"""


# ======================================================================================
# Pages
# ======================================================================================
def landing():
    st.markdown(CSS, unsafe_allow_html=True)
    st.write("")
    st.markdown(
        '<p class="pt-eyebrow">Supply chain tools</p>'
        '<h1 class="pt-h1">Plan inventory and control fulfillment — <em>in one place</em>.</h1>'
        '<p class="pt-lead">Pick a tool to continue. Each tool has its own sign-in, so planners and the '
        'fulfillment team only see what they need.</p>', unsafe_allow_html=True)
    st.write("")
    cols = st.columns(2, gap="large")
    for col, (key, t) in zip(cols, TOOLS.items()):
        with col:
            user = ss["portal_auth"].get(key)
            status = (f'<div class="pt-in">● Signed in as {user["name"]}</div>' if user
                      else '<div class="pt-out">Not signed in</div>')
            st.markdown(
                f'<div class="pt-card"><div class="pt-top"><div class="pt-ico">{t["emoji"]}</div>'
                f'<div><div class="pt-k">{t["kicker"]}</div><div class="pt-name">{t["name"]}</div></div></div>'
                f'<span class="pt-who">{t["who"]}</span><p class="pt-sum">{t["summary"]}</p>'
                f'<ul>{"".join(f"<li>{c}</li>" for c in t["can"])}</ul>{status}</div>',
                unsafe_allow_html=True)
            if user:
                b1, b2 = st.columns([2, 1])
                b1.button(f"Open {t['name']} →", key=f"portal_open_{key}", type="primary", width="stretch",
                          on_click=go, args=(f"app:{key}",))
                b2.button("Sign out", key=f"portal_out_{key}", width="stretch", on_click=sign_out, args=(key,))
            else:
                st.button(f"Sign in to {t['name']}", key=f"portal_in_{key}", type="primary", width="stretch",
                          on_click=go, args=(f"login:{key}",))
    st.markdown('<p class="pt-foot">Need access to a tool? Ask your team lead — access is given per tool. '
                'Demo version: sample data only, nothing is sent outside the app.</p>', unsafe_allow_html=True)


def login(key: str):
    t = TOOLS[key]
    st.markdown(CSS, unsafe_allow_html=True)
    st.button("← All tools", key="portal_back", on_click=go, args=("home",))
    st.markdown(f'<div class="pt-login-ico">{t["emoji"]}</div>', unsafe_allow_html=True)
    st.markdown(f"## {t['name']}")
    st.caption(f"{t['kicker']} · sign in with your {t['name']} account")
    with st.form(f"portal_login_{key}", border=True):
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        ok = st.form_submit_button("Sign in", type="primary", width="stretch")
    if ok:
        user = check_login(key, u, p)
        if user:
            ss["portal_auth"][key] = user
            go(f"app:{key}")
            st.rerun()
        st.error("Username or password is incorrect for this tool.")
    _, demo = load_users(key)
    if demo:
        du, _, dp = t["demo"]
        with st.expander("Demo account"):
            st.markdown(f"Username `{du}` · password `{dp}`")
    other = next(k for k in TOOLS if k != key)
    st.caption(f"Looking for {TOOLS[other]['name']}? It has a separate sign-in on the home page.")


def run_tool(key: str):
    t = TOOLS[key]
    user = ss["portal_auth"][key]

    def sidebar():
        st.markdown(f"**{t['emoji']} {t['name']}**")
        st.caption(f"Signed in as {user['name']}")
        c1, c2 = st.columns(2)
        c1.button("All tools", icon=":material/home:", key=f"portal_home_{key}", width="stretch",
                  on_click=go, args=("home",))
        c2.button("Sign out", key=f"portal_signout_{key}", width="stretch", on_click=sign_out, args=(key,))
        st.divider()

    if not t["file"].exists():
        rel = t["file"].relative_to(HERE)
        found = sorted(str(f.relative_to(HERE)) for f in HERE.rglob("*.py") if ".venv" not in f.parts)
        with st.sidebar:
            sidebar()
        st.error(f"**{t['name']}** can't start: the file `{rel}` is missing from the deployed app.\n\n"
                 f"Make sure the `apps` folder is in the repository next to `streamlit_app.py`.")
        st.caption("Python files found: " + ", ".join(f"`{f}`" for f in found))
        return
    try:
        runpy.run_path(str(t["file"]), run_name=f"portal_{key}",
                       init_globals={"PORTAL": {"user": user, "sidebar": sidebar}})
    except Exception as e:  # Streamlit's rerun/stop signals are BaseException, so they pass through
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)  # also in the Streamlit Cloud logs
        st.error(f"{t['name']} hit a problem: **{type(e).__name__}: {e}**")
        c1, c2 = st.columns(2)
        c1.button("Try again", type="primary", key=f"portal_retry_{key}")
        c2.button("Back to all tools", key=f"portal_err_home_{key}", on_click=go, args=("home",))
        with st.expander("Technical details (share with support)"):
            st.code(tb, language=None)


if kind == "app":
    run_tool(tool)
elif kind == "login":
    login(tool)
else:
    landing()
