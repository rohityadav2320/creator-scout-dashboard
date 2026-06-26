"""
Supabase (Postgres) storage for Creator Scout.

Permanent, redeploy-safe storage for scraped reels. Configuration is read from
(in priority order):
  1. Environment variables  SUPABASE_URL / SUPABASE_KEY
  2. .streamlit/secrets.toml  keys  supabase_url / supabase_key
     (this file is also what Streamlit Cloud uses for deployment secrets)

If neither is set, the app silently falls back to local CSV/Excel only — nothing
breaks, you just don't get cloud storage until you add credentials.
"""
import os

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_SECRETS_PATH = os.path.join(_PROJECT_DIR, ".streamlit", "secrets.toml")

_client = None
_config_cache = None

# Columns we persist on scrape. Mirrors the `reels` table schema (see SCHEMA_SQL).
# NOTE: `status`/`notes` are intentionally NOT here — a re-scrape must never reset
# a creator's outreach status. New rows get the DB default; existing rows are left
# untouched because we omit these columns from the upsert payload.
ROW_COLS = ["reel_url", "username", "full_name", "likes", "comments",
            "views", "followers", "engagement_rate", "category", "contact_email", "bio",
            "external_url", "hashtags", "caption", "audio"]

# Outreach pipeline stages (the CRM).
STATUS_OPTIONS = ["To Contact", "Contacted", "Replied",
                  "Negotiating", "Confirmed", "Rejected"]

SCHEMA_SQL = """
create table if not exists reels (
  reel_url      text primary key,
  username      text,
  full_name     text,
  likes         bigint default 0,
  comments      bigint default 0,
  views         bigint default 0,
  followers     bigint default 0,
  engagement_rate float default 0,
  category      text default '',
  contact_email text default '',
  bio           text default '',
  external_url  text default '',
  hashtags      text,
  caption       text,
  audio         text,
  status        text default 'To Contact',
  notes         text default '',
  scraped_by    text default '',
  scraped_from  text default '',
  batch_id      text default '',
  first_scraped timestamptz default now(),
  last_seen     timestamptz default now()
);
""".strip()

# Run this once on an EXISTING table to add the CRM + team columns (idempotent).
ALTER_SQL = """
alter table reels add column if not exists status       text default 'To Contact';
alter table reels add column if not exists notes        text default '';
alter table reels add column if not exists scraped_by   text default '';
alter table reels add column if not exists scraped_from text default '';
alter table reels add column if not exists batch_id     text default '';
alter table reels add column if not exists engagement_rate float default 0;
alter table reels add column if not exists category      text default '';
alter table reels add column if not exists contact_email text default '';
alter table reels add column if not exists bio           text default '';
alter table reels add column if not exists external_url  text default '';
""".strip()


def _read_config():
    """Return (url, key) from env vars or .streamlit/secrets.toml, or ('','')."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()

    if (not url or not key) and os.path.exists(_SECRETS_PATH):
        try:
            import toml
            data = toml.load(_SECRETS_PATH)
            url = url or str(data.get("supabase_url", "")).strip()
            key = key or str(data.get("supabase_key", "")).strip()
        except Exception:
            pass

    _config_cache = (url, key)
    return _config_cache


def is_configured():
    url, key = _read_config()
    return bool(url and key)


def get_client():
    """Lazily create and cache the Supabase client. Returns None if unconfigured."""
    global _client
    if _client is not None:
        return _client
    url, key = _read_config()
    if not url or not key:
        return None
    try:
        from supabase import create_client
        _client = create_client(url, key)
        return _client
    except Exception:
        return None


def _to_row(r):
    likes = int(r.get("likes", 0) or 0)
    comments = int(r.get("comments", 0) or 0)
    followers = int(r.get("followers", 0) or 0)
    er = round((likes + comments) / followers * 100, 2) if followers > 0 else 0.0
    return {
        "reel_url": r.get("reel_url", ""),
        "username": r.get("username", ""),
        "full_name": r.get("full_name", ""),
        "likes": likes,
        "comments": comments,
        "views": int(r.get("views", 0) or 0),
        "followers": followers,
        "engagement_rate": er,
        "category": r.get("category", "") or "",
        "contact_email": r.get("contact_email", "") or "",
        "bio": (r.get("bio", "") or "").replace("\n", " ")[:500],
        "external_url": r.get("external_url", "") or "",
        "hashtags": ", ".join(r.get("hashtags", []) or []),
        "caption": (r.get("caption", "") or "").replace("\n", " ")[:1000],
        "audio": r.get("audio", "") or "",
    }


def upsert_reels(reels, scraped_by="", batch_id="", scraped_from=""):
    """
    Upsert reels into the shared `reels` table, de-duplicated on reel_url.

    Team behaviour (first-finder wins): a reel's scraped_by/scraped_from/batch_id
    are set only the FIRST time it enters the bank. If a teammate later scrapes the
    same reel, its metrics refresh but the original finder + status/notes persist.

    - scraped_by:   the team member who ran the scrape
    - scraped_from: the Instagram account whose feed was opened/scrolled

    Returns (new_count, already_known_count, error_or_None).
    """
    client = get_client()
    if client is None:
        return 0, 0, "Supabase not configured"
    rows = [_to_row(r) for r in reels if r.get("reel_url")]
    if not rows:
        return 0, 0, None
    urls = [r["reel_url"] for r in rows]
    try:
        # 1) Which reel_urls already exist? (chunked to keep request URLs short)
        existing = set()
        for i in range(0, len(urls), 80):
            chunk = urls[i:i + 80]
            resp = client.table("reels").select("reel_url").in_("reel_url", chunk).execute()
            existing.update(r["reel_url"] for r in (resp.data or []))

        # 2) Refresh metrics on all rows. tag/status columns are NOT in this
        #    payload, so existing rows keep their original values.
        client.table("reels").upsert(rows, on_conflict="reel_url").execute()

        # 3) Tag only the NEW rows with who found them, which feed, and the batch.
        new_urls = [u for u in urls if u not in existing]
        payload = {}
        if scraped_by:
            payload["scraped_by"] = scraped_by
        if scraped_from:
            payload["scraped_from"] = scraped_from
        if batch_id:
            payload["batch_id"] = batch_id
        if new_urls and payload:
            for i in range(0, len(new_urls), 80):
                chunk = new_urls[i:i + 80]
                client.table("reels").update(payload).in_("reel_url", chunk).execute()

        return len(new_urls), len(existing), None
    except Exception as e:
        return 0, 0, f"{type(e).__name__}: {e}"


def update_status(reel_url, status=None, notes=None):
    """Update a single creator's outreach status and/or notes by reel_url.
    Returns (ok, error)."""
    client = get_client()
    if client is None:
        return False, "Supabase not configured"
    payload = {}
    if status is not None:
        payload["status"] = status
    if notes is not None:
        payload["notes"] = notes
    if not payload:
        return True, None
    try:
        client.table("reels").update(payload).eq("reel_url", reel_url).execute()
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def save_status_changes(changes):
    """Persist a list of {reel_url, status, notes} changes. Returns (n_saved, error)."""
    saved = 0
    for c in changes:
        ok, err = update_status(c.get("reel_url"), c.get("status"), c.get("notes"))
        if not ok:
            return saved, err
        saved += 1
    return saved, None


def fetch_known_profiles():
    """Return {username: {followers, category, contact_email, bio, external_url}}
    for every creator already in the DB. Used to skip re-enriching known creators."""
    client = get_client()
    if client is None:
        return {}
    try:
        resp = (
            client.table("reels")
            .select("username,followers,category,contact_email,bio,external_url,engagement_rate,scraped_by")
            .execute()
        )
        result = {}
        for r in (resp.data or []):
            u = r.get("username", "")
            # Only cache if we actually have follower data (enrichment was done)
            if u and r.get("followers", 0):
                result[u] = {
                    "followers":      int(r.get("followers", 0) or 0),
                    "category":       r.get("category", "") or "",
                    "contact_email":  r.get("contact_email", "") or "",
                    "bio":            r.get("bio", "") or "",
                    "external_url":   r.get("external_url", "") or "",
                    "engagement_rate": float(r.get("engagement_rate", 0) or 0),
                    "scraped_by":     r.get("scraped_by", "") or "",
                }
        return result
    except Exception:
        return {}


def fetch_all_reels(limit=2000):
    """Fetch stored reels (most recently seen first).
    Returns (rows, error) — error is None on success, else a human-readable string.
    A connection error is reported distinctly from a genuinely empty table."""
    global _client
    client = get_client()
    if client is None:
        return [], "Supabase not configured"
    try:
        resp = (
            client.table("reels")
            .select("*")
            .order("last_seen", desc=True)
            .limit(limit)
            .execute()
        )
        return (resp.data or []), None
    except Exception as e:
        # Drop the cached client so the next attempt reconnects cleanly.
        _client = None
        msg = str(e)
        if "nodename nor servname" in msg or "ConnectError" in type(e).__name__:
            return [], "Could not reach Supabase (network/DNS issue). Check your internet and try Refresh again."
        return [], f"{type(e).__name__}: {msg}"


# ─────────────────────────────────────────────────────────────────────────────
# JOB QUEUE — the web dashboard queues jobs here; local agents pick them up,
# scrape locally (real browser + login), and write results back to `reels`.
# ─────────────────────────────────────────────────────────────────────────────
from datetime import datetime, timezone

JOBS_SQL = """
create table if not exists jobs (
  id            bigint generated by default as identity primary key,
  type          text not null,                 -- 'hashtag' | 'reference'
  params        jsonb not null default '{}',   -- {hashtags:[], seeds:[], max:50, enrich:true, depth:1}
  account_label text default '',               -- IG account/agent that should run it ('' = any)
  status        text default 'queued',         -- queued | running | done | error
  created_by    text default '',               -- team member who queued it
  agent_label   text default '',               -- agent that picked it up
  progress      text default '',
  result_count  int default 0,
  error         text default '',
  created_at    timestamptz default now(),
  started_at    timestamptz,
  finished_at   timestamptz
);

create table if not exists agents (
  label      text primary key,
  last_seen  timestamptz default now()
);
""".strip()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def create_job(job_type, params, account_label="", created_by=""):
    """Queue a scrape job. Returns (job_id, error)."""
    client = get_client()
    if client is None:
        return None, "Supabase not configured"
    try:
        resp = client.table("jobs").insert({
            "type": job_type,
            "params": params or {},
            "account_label": (account_label or "").strip(),
            "created_by": (created_by or "").strip(),
            "status": "queued",
        }).execute()
        rows = resp.data or []
        return (rows[0].get("id") if rows else None), None
    except Exception as e:
        return None, str(e)


def claim_next_job(agent_label, account_label=""):
    """Claim the oldest queued job this agent should run. Returns job dict or None.
    The status-guarded update makes the claim safe across agents (Postgres serializes
    the UPDATE, so only one agent wins a given row)."""
    client = get_client()
    if client is None:
        return None
    try:
        resp = (client.table("jobs").select("*").eq("status", "queued")
                .order("created_at", desc=False).limit(10).execute())
        for job in (resp.data or []):
            jal = (job.get("account_label") or "").strip()
            # Skip jobs targeted at a different account (blank = anyone can run)
            if jal and account_label and jal != account_label:
                continue
            upd = (client.table("jobs")
                   .update({"status": "running", "agent_label": agent_label,
                            "started_at": _now_iso()})
                   .eq("id", job["id"]).eq("status", "queued").execute())
            if upd.data:               # we won the claim
                return upd.data[0]
        return None
    except Exception:
        return None


def update_job(job_id, status=None, progress=None, result_count=None, error=None):
    client = get_client()
    if client is None:
        return
    payload = {}
    if status is not None:
        payload["status"] = status
    if progress is not None:
        payload["progress"] = str(progress)[:300]
    if result_count is not None:
        payload["result_count"] = int(result_count)
    if error is not None:
        payload["error"] = str(error)[:500]
    if status in ("done", "error"):
        payload["finished_at"] = _now_iso()
    if not payload:
        return
    try:
        client.table("jobs").update(payload).eq("id", job_id).execute()
    except Exception:
        pass


def fetch_jobs(limit=50):
    client = get_client()
    if client is None:
        return []
    try:
        resp = (client.table("jobs").select("*")
                .order("created_at", desc=True).limit(limit).execute())
        return resp.data or []
    except Exception:
        return []


def agent_heartbeat(label):
    client = get_client()
    if client is None or not label:
        return
    try:
        client.table("agents").upsert({"label": label, "last_seen": _now_iso()}).execute()
    except Exception:
        pass


def fetch_agents():
    client = get_client()
    if client is None:
        return []
    try:
        resp = client.table("agents").select("*").order("last_seen", desc=True).execute()
        return resp.data or []
    except Exception:
        return []
