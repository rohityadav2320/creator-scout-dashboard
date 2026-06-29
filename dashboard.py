"""
Creator Scout — Web Dashboard
Clean Trendwell-style UI. Queues jobs for local agents to pick up and run.
No browser, no Playwright — pure Supabase reads/writes.
"""
import io
import time
from datetime import datetime, timezone

import streamlit as st
import pandas as pd

import db
from hashtag_library import HASHTAG_LIBRARY

st.set_page_config(page_title="Creator Scout", page_icon="🎬", layout="wide")

# ── CSS — clean dark style ────────────────────────────────────────────────────
st.markdown("""
<style>
[data-testid="stSidebar"] { background: #0f0f0f; }
.block-container { padding-top: 2rem; max-width: 1100px; }
div[data-testid="metric-container"] { background: #1a1a1a; border-radius: 8px; padding: 12px; }
.stButton > button { border-radius: 8px; font-weight: 600; }
.stButton > button[kind="primary"] { background: #6366f1; border: none; }
.stButton > button[kind="primary"]:hover { background: #4f46e5; }
</style>
""", unsafe_allow_html=True)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _agent_online(last_seen, within=40):
    if not last_seen:
        return False
    try:
        ts = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds() < within
    except Exception:
        return False


def to_excel(df):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Creators")
    return buf.getvalue()


def push_to_gsheet(creators):
    import requests
    try:
        url = st.secrets.get("gsheet_webapp_url", "")
    except Exception:
        url = ""
    if not url:
        return False, "Google Sheet not connected."
    if not creators:
        return False, "No creators selected."
    try:
        r = requests.post(url, json={"creators": creators}, timeout=25)
        if r.status_code in (200, 302):
            try:
                d = r.json()
                msg = f"Added {d.get('added', len(creators))} to sheet"
                if d.get("skipped"):
                    msg += f" · skipped {d['skipped']} (already there)"
                return True, msg + "."
            except Exception:
                return True, f"Sent {len(creators)} to sheet."
        return False, f"Sheet responded {r.status_code}."
    except Exception as e:
        return False, f"Could not reach sheet: {e}"


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🎬 Creator Scout")
    st.caption("Reel discovery")
    st.divider()

    page = st.radio(
        "Navigate",
        ["🏠 Home", "➕ New Scrape", "📋 Jobs", "🗄️ Creators / CRM", "📲 Install Agent"],
        label_visibility="collapsed",
    )

    st.divider()

    # Agent status
    if db.is_configured():
        agents = db.fetch_agents()
        online = [a for a in agents if _agent_online(a.get("last_seen"))]
        if online:
            for a in online:
                st.markdown(f"🟢 **{a.get('label', 'agent')}**")
        else:
            st.markdown("⚪ No agents online")
    else:
        st.error("Supabase not connected.")


# ── Page: Home ────────────────────────────────────────────────────────────────
if page == "🏠 Home":
    st.title("Creator Scout")
    st.caption("Find Instagram creators for your brand — fast.")
    st.divider()

    if not db.is_configured():
        st.warning("Add Supabase credentials to Streamlit secrets to get started.")
        st.stop()

    # Quick stats
    rows, _ = db.fetch_all_reels(limit=5000)
    df = pd.DataFrame(rows) if rows else pd.DataFrame()
    total = len(df)
    confirmed = len(df[df["status"] == "Confirmed"]) if total else 0
    contacted = len(df[df["status"] == "Contacted"]) if total else 0
    to_contact = len(df[df["status"] == "To Contact"]) if total else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Creators", total)
    c2.metric("To Contact", to_contact)
    c3.metric("Contacted", contacted)
    c4.metric("Confirmed", confirmed)

    st.divider()

    # Recent jobs
    st.subheader("Recent jobs")
    jobs = db.fetch_jobs(limit=5)
    if jobs:
        STATUS_ICON = {"queued": "🕒", "running": "⏳", "done": "✅", "error": "❌"}
        for j in jobs:
            p = j.get("params") or {}
            target = ", ".join(p.get("hashtags") or p.get("seeds") or ["feed"])
            st.markdown(
                f"{STATUS_ICON.get(j.get('status'), '')} **{j.get('type', '').title()}** — "
                f"{target[:50]} &nbsp;·&nbsp; {j.get('result_count', 0)} creators "
                f"&nbsp;·&nbsp; by {j.get('created_by', '—')}"
            )
    else:
        st.info("No jobs yet. Go to **New Scrape** to start.")


# ── Page: New Scrape ──────────────────────────────────────────────────────────
elif page == "➕ New Scrape":
    st.title("New scrape")
    st.caption("Queue a run. Your local agent picks it up automatically.")

    if not db.is_configured():
        st.warning("Supabase not connected.")
        st.stop()

    agents = db.fetch_agents()
    online_agents = [a for a in agents if _agent_online(a.get("last_seen"))]
    online_labels = [a.get("label", "") for a in online_agents]

    col_form, col_how = st.columns([3, 2], gap="large")

    with col_form:
        st.markdown("#### Configure run")
        st.caption("Defaults come from the category — tweak just for this run if you like.")

        scrape_type = st.selectbox(
            "Scrape type",
            ["Hashtag search", "Reference creator", "📱 Trained Feed"],
            label_visibility="visible",
        )

        if scrape_type == "Hashtag search":
            category = st.selectbox("Category", ["— pick a category —"] + list(HASHTAG_LIBRARY.keys()))
            if category != "— pick a category —":
                default_kws = ", ".join(HASHTAG_LIBRARY[category][:6])
            else:
                default_kws = ""
            keywords = st.text_input("Keywords / Hashtags", value=default_kws,
                                     placeholder="comedy, funnyreels, skit")

            c1, c2 = st.columns(2)
            with c1:
                max_creators = st.number_input("How many to find", 10, 500, 50, step=10)
            with c2:
                min_followers = st.number_input("Min followers", 0, 1_000_000, 10000, step=1000)

            enrich = st.checkbox("Fetch followers + email", value=True)

        elif scrape_type == "Reference creator":
            seeds_in = st.text_input("Seed creator username(s)",
                                     placeholder="e.g. some_creator, another_creator")
            c1, c2 = st.columns(2)
            with c1:
                max_creators = st.number_input("How many to find", 10, 500, 100, step=10)
            with c2:
                depth = st.select_slider("Similar-creator depth", options=[0, 1, 2], value=1)

        else:  # Trained Feed
            st.info(
                "Opens your trained Instagram account's /reels/ feed in the browser and "
                "scrolls to collect creators. Train your burner account once on your phone "
                "by watching niche reels — the algorithm remembers. "
                "Scraping here does NOT change the feed (no watch signals sent)."
            )
            c1, c2 = st.columns(2)
            with c1:
                max_creators = st.number_input("Max reels to scan", 10, 300, 50, step=10)
            with c2:
                min_followers = st.number_input("Min followers", 0, 1_000_000, 10000, step=1000)

        st.divider()

        # Instagram account to scrape WITH
        ig_account = st.text_input(
            "Instagram account to scrape with",
            placeholder="e.g. my_burner_account",
            help="The Instagram login used for this scrape. First time you use a new account, "
                 "a Chrome window opens — log in once by hand. After that the login is saved "
                 "and reused automatically.",
        )

        # Agent (laptop) + name row
        a1, a2 = st.columns(2)
        with a1:
            acct = st.selectbox(
                "Run on (your laptop)",
                options=(online_labels or ["(no agent online)"]),
                help="Which team laptop runs the scrape. This is the machine, not the Instagram account.",
            )
            if online_agents:
                st.markdown(f"🟢 Online: **{acct}**")
            else:
                st.markdown("⚪ No agents online — start the agent first")
        with a2:
            me = st.text_input("Your name", placeholder="e.g. Priya")

        # Queue button
        if st.button("▶ Start scrape", type="primary", use_container_width=True):
            ig = ig_account.strip().lstrip("@")
            if not online_labels:
                st.error("No agent online. Start the agent on your laptop first.")
            elif not ig:
                st.error("Enter the Instagram account to scrape with.")
            else:
                if scrape_type == "Hashtag search":
                    tags = [t.strip().lstrip("#") for t in keywords.replace(",", " ").split() if t.strip()]
                    if not tags:
                        st.error("Enter at least one keyword/hashtag.")
                    else:
                        jid, err = db.create_job(
                            "hashtag",
                            {"hashtags": tags, "max": int(max_creators),
                             "enrich": bool(enrich), "min_followers": int(min_followers),
                             "ig_account": ig},
                            account_label=acct, created_by=me)
                        if not err:
                            st.success(f"✅ Queued! Job #{jid} on **{acct}** as **@{ig}** — {', '.join(tags[:3])}")
                        else:
                            st.error(f"❌ {err}")

                elif scrape_type == "Reference creator":
                    seeds = [s.strip().lstrip("@") for s in seeds_in.split(",") if s.strip()]
                    if not seeds:
                        st.error("Enter at least one seed creator username.")
                    else:
                        jid, err = db.create_job(
                            "reference",
                            {"seeds": seeds, "max": int(max_creators), "depth": int(depth),
                             "ig_account": ig},
                            account_label=acct, created_by=me)
                        if not err:
                            st.success(f"✅ Queued! Job #{jid} on **{acct}** as **@{ig}** — @{', @'.join(seeds)}")
                        else:
                            st.error(f"❌ {err}")

                else:  # Trained Feed
                    jid, err = db.create_job(
                        "trained_feed",
                        {"max": int(max_creators), "min_followers": int(min_followers),
                         "ig_account": ig},
                        account_label=acct, created_by=me)
                    if not err:
                        st.success(f"✅ Queued! Trained feed job #{jid} on **{acct}** as **@{ig}**")
                    else:
                        st.error(f"❌ {err}")

    with col_how:
        st.markdown("#### ✦ How it works")
        st.caption("What happens after you click start.")
        st.markdown("""
**1 — Queued**
Recorded instantly — this page never freezes.

**2 — Picked up**
Your laptop's agent sees the job within seconds.

**3 — Scraping**
Reels found on your own WiFi, your own session.

**4 — Synced**
Results land here under Creators / CRM.
        """)

        if online_agents:
            st.divider()
            st.markdown("**Active agents**")
            for a in online_agents:
                st.markdown(f"🟢 {a.get('label', 'agent')}")


# ── Page: Jobs ────────────────────────────────────────────────────────────────
elif page == "📋 Jobs":
    st.title("Jobs")
    if not db.is_configured():
        st.warning("Supabase not connected.")
        st.stop()

    if st.button("🔄 Refresh"):
        st.rerun()

    jobs = db.fetch_jobs(limit=100)
    if not jobs:
        st.info("No jobs yet. Queue one from **New Scrape**.")
    else:
        STATUS_ICON = {"queued": "🕒", "running": "⏳", "done": "✅", "error": "❌"}
        rows = []
        for j in jobs:
            p = j.get("params") or {}
            target = ", ".join(p.get("hashtags") or p.get("seeds") or ["feed"])
            rows.append({
                "#": j.get("id"),
                "Status": f"{STATUS_ICON.get(j.get('status'), '')} {j.get('status', '')}",
                "Type": j.get("type", ""),
                "Target": target[:60],
                "Results": j.get("result_count", 0),
                "Account": j.get("account_label", ""),
                "By": j.get("created_by", ""),
                "Progress / Error": (j.get("error") or j.get("progress") or "")[:80],
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.caption("Jobs stay 🕒 queued until an agent for that account is online.")


# ── Page: Creators / CRM ──────────────────────────────────────────────────────
elif page == "🗄️ Creators / CRM":
    st.title("Creators")
    if not db.is_configured():
        st.warning("Supabase not connected.")
        st.stop()

    if st.button("🔄 Refresh", key="db_refresh") or "dash_rows" not in st.session_state:
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
        for col, d in [("status", "To Contact"), ("notes", ""), ("contact_email", ""), ("category", "")]:
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

        st.divider()

        # Filters
        f1, f2 = st.columns([1, 2])
        with f1:
            sf = st.selectbox("Status", ["All"] + db.STATUS_OPTIONS)
        with f2:
            q = st.text_input("🔎 Search username / caption", "")

        view = df.copy()
        if sf != "All":
            view = view[view["status"] == sf]
        if q:
            ql = q.lower()
            view = view[view.apply(
                lambda r: ql in str(r.get("username", "")).lower()
                or ql in str(r.get("caption", "")).lower(), axis=1)]

        show_cols = ["status", "→ Sheet", "profile", "username", "followers",
                     "engagement_rate", "contact_email", "category", "notes", "reel_url"]
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
                    st.info("No changes to save.")
                else:
                    n, e = db.save_status_changes(changes)
                    st.success(f"✅ Saved {n} update(s).") if not e else st.error(e)
                    st.session_state.pop("dash_rows", None)
        with b2:
            sheet_rows = edited[edited["→ Sheet"] == True]  # noqa
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


# ── Page: Install Agent ───────────────────────────────────────────────────────
elif page == "📲 Install Agent":
    st.title("Install Agent")
    st.caption("Set up your laptop to run scrapes. One-time setup, then just double-click.")

    st.markdown("---")
    st.markdown("### Step 1 — Download the folder")
    st.info("Ask Rohit to share the `CreatorScout` folder (Google Drive / zip).")

    st.markdown("### Step 2 — Edit config.txt")
    st.markdown("Open `config.txt` inside the folder and change only your name:")
    st.code("""NAME=YourName
SUPABASE_URL=<ask Rohit for the URL>
SUPABASE_KEY=<ask Rohit for the key>""", language="text")

    st.markdown("### Step 3 — Install Python (if not already)")
    st.markdown("Download from [python.org/downloads](https://www.python.org/downloads/) → install → done.")

    st.markdown("### Step 4 — Double-click to run")
    st.markdown("Double-click **`CreatorScout-Agent.command`** → terminal opens → first time takes ~2 min to setup.")

    st.markdown("### Step 5 — First scrape = Instagram login")
    st.markdown(
        "Queue a scrape from **New Scrape**. A Chrome window opens — log into your Instagram burner account. "
        "Login is saved — you won't need to do this again."
    )

    st.divider()
    st.markdown("**Stopping & restarting**")
    st.markdown(
        "Minimise the terminal — don't close it while scraping. "
        "To stop: click terminal → **Ctrl + C**. "
        "To restart: double-click `CreatorScout-Agent.command` again."
    )
    st.caption("Your Instagram login is saved locally on your machine. It never leaves your computer.")
