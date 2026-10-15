# Supply Chain Tools

One Streamlit app with a shared landing page and a **separate sign-in for each tool**:

| Tool | What it does | Who uses it |
|---|---|---|
| **Allocation Planner** | Review the monthly COM allocation plan, apply the planner input (storage types, min / max targets, building capacity, departments) and publish it. | Planners |
| **Dynamic Fulfillment** | Exclude products from a fulfillment process (CS Margin, Sellout Risk, Replenishment, VDF Margin), bring them back, or check a product's status. | Fulfillment team |

Signing in to one tool does **not** give access to the other. Signing out of one tool keeps the other signed in.

## Run
```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```
Demo accounts (used only until real users are added):

| Tool | Username | Password |
|---|---|---|
| Allocation Planner | `yogesh` | `planner123` |
| Dynamic Fulfillment | `df.user` | `df@123` |

## Users
```bash
python streamlit_app.py adduser alloc <username> "<Full Name>" <password>
python streamlit_app.py adduser df    <username> "<Full Name>" <password>
```
Paste the printed block into `.streamlit/secrets.toml` (see `secrets.toml.example`) or Streamlit Cloud Secrets.
The Allocation Planner's original `[users.*]` block still works for planner accounts.

## Files
```
streamlit_app.py              landing page, sign-in per tool, routing
apps/allocation_planner.py    Allocation Planner (from final_demo_com_allocation)
apps/dynamic_fulfillment.py   Dynamic Fulfillment product exclusion tool (from dfui)
```
Both tools run in demo mode by default (sample data, nothing sent outside the app).
- Allocation Planner → real BigQuery: add `[gcp_service_account]` and `[bigquery]` to Secrets.
- Dynamic Fulfillment → real BigQuery: `streamlit run streamlit_app.py -- --backend bigquery --env dev`

## Deploy on Streamlit Community Cloud
Push this folder to GitHub → share.streamlit.io → Create app → main file `streamlit_app.py` → add Secrets → Deploy.
