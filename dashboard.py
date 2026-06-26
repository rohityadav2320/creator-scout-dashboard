"""
Creator Scout — Web Dashboard (the hosted control plane).

This is the SHARED web app the team opens (one URL). It does NOT scrape and has
NO browser — it only:
  • queues scrape jobs (which local agents pick up and run),
  • shows job status + which agents are online,
  • shows the shared creator database + CRM (status/notes), and
  • pushes selected creators to the Google Sheet.

Because it never touches Playwright, it deploys cleanly to Streamlit Cloud.
The local scraping portal (app.py) and the agent (agent.py) are unaffected.

Run locally:   python3 -m streamlit run dashboard.py --server.port 8504
"""
import io
import time
from datetime import datetime, timezone

import streamlit as st
import pandas as pd

import db

st.set_page_config(page_title="Creator Scout — Dashboard", page_icon="🎬", layout="wide")


# ── Helpers ──────────────────────────────────────────────────────────────────
def to_excel(df):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Creators")
    return buf.getvalue()


def to_csv(df):
    return df.to_csv(index=False).encode("utf-8")


def push_to_gsheet(creators):
    import requests
    try:
        url = st.secrets.get("gsheet_webapp_url", "")
    except Exception:
        url = ""
    if not url:
        return False, "Google Sheet not connected (add gsheet_webapp_url to secrets)."
    if not creators:
        return False, "No creators selected."
    try:
        r = requests.post(url, json={"creators": creators}, timeout=25)
        if r.status_code in (200, 302):
            try:
                d = r.json()
                msg = f"Added {d.get('added', len(creators))} to the sheet"
                if d.get("skipped"):
                    msg += f" · skipped {d['skipped']} (already there)"
                return True, msg + "."
            except Exception:
                return True, f"Sent {len(creators)} to the sheet."
        return False, f"Sheet responded {r.status_code}."
    except Exception as e:
        return False, f"Could not reach the sheet: {e}"


def _agent_online(last_seen, within=40):
    if not last_seen:
        return False
    try:
        ts = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds() < within
    except Exception:
        return False


# ── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🎬 Creator Scout")
    st.caption("Team Dashboard")
    st.divider()
    me = st.text_input("Your name", placeholder="e.g. Rohit",
                       help="Tags the jobs you queue + creators you save.")
    st.divider()
    if not db.is_configured():
        st.error("Supabase not connected. Add credentials to secrets.")
    else:
        agents = db.fetch_agents()
        online = [a for a in agents if _agent_online(a.get("last_seen"))]
        st.subheader("🖥️ Agents")
        if online:
            for a in online:
                st.markdown(f"🟢 **{a.get('label','agent')}**")
        else:
            st.markdown("⚪ No agents online")
        st.caption("Agents are team laptops running `agent.py`. A scrape needs at "
                   "least one online agent for its account.")

st.title("Creator Scout — Dashboard")

if not db.is_configured():
    st.warning("Connect Supabase (secrets.toml) to use the dashboard.")
    st.stop()

tab_new, tab_jobs, tab_db = st.tabs(["➕ New Scrape", "📋 Jobs", "🗄️ Creators / CRM"])

# ── Tab: New Scrape (queue a job) ────────────────────────────────────────────
with tab_new:
    st.subheader("Queue a scrape")
    st.caption("This creates a job. An online agent picks it up, scrapes on its "
               "own machine (real login), and the results appear under Creators.")

    jtype = st.radio("Type", ["Hashtag search", "Reference creator"], horizontal=True)
    agents = db.fetch_agents()
    online_labels = [a.get("label", "") for a in agents if _agent_online(a.get("last_seen"))]
    acct = st.selectbox(
        "Run on agent / account",
        options=(online_labels or ["(no agent online)"]),
        help="Which team machine + Instagram account should run this scrape.",
    )

    if jtype == "Hashtag search":
        tags_in = st.text_area("Hashtags (comma or space separated)",
                               placeholder="tamilskit, tamilcomedy, chennaicomedy")
        c1, c2 = st.columns(2)
        with c1:
            hmax = st.number_input("Max creators", 10, 500, 50, step=10, key="hmax")
        with c2:
            henrich = st.checkbox("Fetch followers + email", value=True, key="henrich")
        if st.button("➕ Queue hashtag scrape", type="primary", use_container_width=True):
            tags = [t.strip().lstrip("#") for t in tags_in.replace(",", " ").split() if t.strip()]
            if not online_labels:
                st.error("No agent is online. Start `agent.py` on a team machine first.")
            elif not tags:
                st.error("Enter at least one hashtag.")
            else:
                jid, err = db.create_job(
                    "hashtag",
                    {"hashtags": tags, "max": int(hmax), "enrich": bool(henrich)},
                    account_label=acct, created_by=me)
                if not err:
                    st.success(f"✅ Queued job #{jid} for **{acct}** — {', '.join(tags)}")
                else:
                    st.error(f"❌ {err}")
    else:
        seeds_in = st.text_area("Seed creator username(s) (comma separated)",
                                placeholder="some_creator, another_creator")
        c1, c2 = st.columns(2)
        with c1:
            rmax = st.number_input("Max creators", 10, 500, 100, step=10, key="rmax")
        with c2:
            rdepth = st.select_slider("Similar-creator depth", options=[0, 1, 2], value=1,
                                      key="rdepth")
        if st.button("➕ Queue reference scrape", type="primary", use_container_width=True):
            seeds = [s.strip().lstrip("@") for s in seeds_in.split(",") if s.strip()]
            if not online_labels:
                st.error("No agent is online. Start `agent.py` on a team machine first.")
            elif not seeds:
                st.error("Enter at least one seed creator.")
            else:
                jid, err = db.create_job(
                    "reference",
                    {"seeds": seeds, "max": int(rmax), "depth": int(rdepth)},
                    account_label=acct, created_by=me)
                if not err:
                    st.success(f"✅ Queued job #{jid} for **{acct}** — like @{', @'.join(seeds)}")
                else:
                    st.error(f"❌ {err}")

# ── Tab: Jobs ────────────────────────────────────────────────────────────────
with tab_jobs:
    top = st.columns([1, 4])
    with top[0]:
        if st.button("🔄 Refresh", use_container_width=True, key="jobs_refresh"):
            st.rerun()
    jobs = db.fetch_jobs(limit=100)
    if not jobs:
        st.info("No jobs yet. Queue one from **New Scrape**.")
    else:
        STATUS_ICON = {"queued": "🕒", "running": "⏳", "done": "✅", "error": "❌"}
        rows = []
        for j in jobs:
            p = j.get("params") or {}
            target = ", ".join(p.get("hashtags") or p.get("seeds") or [])
            rows.append({
                "#": j.get("id"),
                "Status": f"{STATUS_ICON.get(j.get('status'),'')} {j.get('status','')}",
                "Type": j.get("type", ""),
                "Target": target[:60],
                "Results": j.get("result_count", 0),
                "Account": j.get("account_label", ""),
                "By": j.get("created_by", ""),
                "Agent": j.get("agent_label", ""),
                "Progress / Error": (j.get("error") or j.get("progress") or "")[:80],
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.caption("Tip: a job stays 🕒 queued until an agent for that account is online.")

# ── Tab: Creators / CRM ──────────────────────────────────────────────────────
with tab_db:
    top = st.columns([1, 3])
    with top[0]:
        refresh = st.button("🔄 Refresh", use_container_width=True, key="db_refresh")
    if refresh or "dash_rows" not in st.session_state:
        with st.spinner("Loading creators…"):
            st.session_state.dash_rows, st.session_state.dash_err = db.fetch_all_reels(limit=5000)

    rows = st.session_state.get("dash_rows", [])
    err = st.session_state.get("dash_err")
    if err:
        st.error(err)
    elif not rows:
        st.info("No creators yet. Queue a scrape and let an agent run it.")
    else:
        df = pd.DataFrame(rows)
        for col, d in [("status", "To Contact"), ("notes", ""), ("scraped_by", ""),
                       ("contact_email", ""), ("category", "")]:
            if col not in df.columns:
                df[col] = d
        df["status"] = df["status"].fillna("To Contact").replace("", "To Contact")
        df["notes"] = df["notes"].fillna("")
        df["profile"] = df.get("username", "").fillna("").apply(
            lambda u: f"https://www.instagram.com/{u}/" if u else "")

        # Pipeline metrics
        counts = df["status"].value_counts().to_dict()
        pcols = st.columns(len(db.STATUS_OPTIONS))
        for i, s in enumerate(db.STATUS_OPTIONS):
            pcols[i].metric(s, counts.get(s, 0))

        # Filters
        f1, f2 = st.columns([1, 2])
        with f1:
            sf = st.selectbox("Status", ["All"] + db.STATUS_OPTIONS, key="dash_sf")
        with f2:
            q = st.text_input("🔎 Search username / caption", "", key="dash_q")
        view = df
        if sf != "All":
            view = view[view["status"] == sf]
        if q:
            ql = q.lower()
            view = view[view.apply(
                lambda r: ql in str(r.get("username", "")).lower()
                or ql in str(r.get("caption", "")).lower(), axis=1)]

        show_cols = ["status", "→ Sheet", "profile", "username", "followers",
                     "engagement_rate", "contact_email", "category", "notes", "reel_url"]
        view = view.copy()
        view["→ Sheet"] = False
        view = view[[c for c in show_cols if c in view.columns]]

        edited = st.data_editor(
            view, use_container_width=True, hide_index=True, key="dash_editor",
            column_config={
                "status": st.column_config.SelectboxColumn("Status", options=db.STATUS_OPTIONS),
                "→ Sheet": st.column_config.CheckboxColumn("→ Sheet"),
                "notes": st.column_config.TextColumn("Notes", width="large"),
                "profile": st.column_config.LinkColumn("Profile", display_text=r"instagram\.com/([^/]+)"),
                "reel_url": st.column_config.LinkColumn("Reel", display_text="open ↗"),
                "engagement_rate": st.column_config.NumberColumn("ER %", format="%.1f%%"),
            },
            disabled=[c for c in view.columns if c not in ("status", "notes", "→ Sheet")],
        )

        b1, b2, b3 = st.columns(3)
        with b1:
            if st.button("💾 Save status/notes", type="primary", use_container_width=True):
                orig = {r["reel_url"]: r for _, r in view.iterrows()}
                changes = []
                for _, r in edited.iterrows():
                    o = orig.get(r["reel_url"])
                    if o is not None and (str(r.get("status")) != str(o.get("status"))
                                          or str(r.get("notes", "")) != str(o.get("notes", ""))):
                        changes.append({"reel_url": r["reel_url"], "status": r.get("status"),
                                        "notes": r.get("notes", "")})
                if not changes:
                    st.info("No changes.")
                else:
                    n, e = db.save_status_changes(changes)
                    st.success(f"✅ Saved {n} update(s).") if not e else st.error(e)
                    st.session_state.pop("dash_rows", None)
        with b2:
            sheet_rows = edited[edited["→ Sheet"] == True]  # noqa: E712
            if st.button(f"📤 Send {len(sheet_rows)} → Sheet", use_container_width=True,
                         disabled=len(sheet_rows) == 0):
                payload = [{"name": r.get("username"), "username": r.get("username"),
                            "email": r.get("contact_email", ""), "language": ""}
                           for _, r in sheet_rows.iterrows()]
                ok, msg = push_to_gsheet(payload)
                st.success(f"✅ {msg}") if ok else st.error(f"❌ {msg}")
        with b3:
            st.download_button("⬇️ Excel", data=to_excel(view),
                               file_name="creators.xlsx", use_container_width=True)
