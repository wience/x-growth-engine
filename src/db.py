"""Supabase wrapper. Owns the tweet_queue state machine and analytics tables.

State machine for tweet_queue.status:
    draft → approved → posting → posted
                    ↘ rejected        ↘ failed

The claim step (claim_next_approved) is a Postgres RPC using
FOR UPDATE SKIP LOCKED, so two overlapping scheduler runs can never claim the
same row. See scripts/init_db.sql.
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config

log = logging.getLogger(__name__)

_TZ = ZoneInfo(config.TIMEZONE)


def _now_iso() -> str:
    return datetime.now(_TZ).isoformat()


class Database:
    """Wraps a supabase-py client. Inject a client for tests."""

    def __init__(self, client):
        self._c = client

    # ── drafts / queue ──────────────────────────────────────────────
    def add_draft(
        self,
        content: str,
        *,
        pillar: str | None = None,
        grounded_in: str | None = None,
        slot: str | None = None,
        format: str = "single",
        thread_parent_id: str | None = None,
        thread_index: int = 0,
        source: str = "routine",
        tone: str | None = None,
        hook_type: str | None = None,
        score: int | None = None,
    ) -> dict:
        row = {
            "content": content,
            "pillar": pillar,
            "grounded_in": grounded_in,
            "slot": slot,
            "format": format,
            "thread_parent_id": thread_parent_id,
            "thread_index": thread_index,
            "source": source,
            "tone": tone,
            "hook_type": hook_type,
            "score": score,
            "status": "draft",
        }
        resp = self._c.table("tweet_queue").insert(row).execute()
        return resp.data[0] if resp.data else {}

    def get_drafts(self, status: str = "draft", limit: int = 5) -> list[dict]:
        resp = (
            self._c.table("tweet_queue")
            .select("*")
            .eq("status", status)
            .order("created_at")
            .limit(limit)
            .execute()
        )
        return resp.data or []

    def set_status(self, draft_id: str, status: str) -> dict:
        resp = (
            self._c.table("tweet_queue")
            .update({"status": status})
            .eq("id", draft_id)
            .execute()
        )
        return resp.data[0] if resp.data else {}

    def update_content(self, draft_id: str, content: str) -> dict:
        resp = (
            self._c.table("tweet_queue")
            .update({"content": content})
            .eq("id", draft_id)
            .execute()
        )
        return resp.data[0] if resp.data else {}

    def claim_next_approved(self) -> dict | None:
        """Atomically flip the next approved single/thread_start to 'posting'.

        Returns the claimed row, or None if nothing is approved. Backed by a
        Postgres RPC so concurrent runs can't double-claim.
        """
        resp = self._c.rpc("claim_next_approved", {}).execute()
        data = resp.data
        if not data:
            return None
        return data[0] if isinstance(data, list) else data

    def get_thread_children(self, parent_id: str) -> list[dict]:
        """Approved thread_reply rows for a parent, ordered by thread_index."""
        resp = (
            self._c.table("tweet_queue")
            .select("*")
            .eq("thread_parent_id", parent_id)
            .order("thread_index")
            .execute()
        )
        return resp.data or []

    def mark_posted(self, draft_id: str, tweet_id: str) -> dict:
        resp = (
            self._c.table("tweet_queue")
            .update(
                {
                    "status": "posted",
                    "tweet_id": str(tweet_id),
                    "posted_at": _now_iso(),
                }
            )
            .eq("id", draft_id)
            .execute()
        )
        return resp.data[0] if resp.data else {}

    def mark_failed(self, draft_id: str, error: str) -> dict:
        resp = (
            self._c.table("tweet_queue")
            .update({"status": "failed", "error": error[:2000]})
            .eq("id", draft_id)
            .execute()
        )
        return resp.data[0] if resp.data else {}

    def get_recent_posted(self, lookback_days: int = 7, limit: int = 50) -> list[dict]:
        cutoff = (datetime.now(_TZ) - timedelta(days=lookback_days)).isoformat()
        resp = (
            self._c.table("tweet_queue")
            .select("id,content,tweet_id,posted_at,pillar,tone,hook_type,slot,format,score")
            .eq("status", "posted")
            .gte("posted_at", cutoff)
            .not_.is_("tweet_id", "null")
            .order("posted_at", desc=True)
            .limit(limit)
            .execute()
        )
        return resp.data or []

    def counts_today(self) -> dict:
        """Counts of drafts (all-time pending) plus today's approved/posted."""
        start = datetime.now(_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        start_iso = start.isoformat()

        def _count(status, since=None):
            q = (
                self._c.table("tweet_queue")
                .select("id", count="exact")
                .eq("status", status)
            )
            if since:
                q = q.gte("created_at", since)
            resp = q.execute()
            return resp.count if resp.count is not None else len(resp.data or [])

        return {
            "draft": _count("draft"),
            "approved": _count("approved"),
            "posted_today": _count("posted", start_iso),
        }

    # ── analytics ───────────────────────────────────────────────────
    def save_daily_analytics(self, metrics: dict) -> dict:
        today = datetime.now(_TZ).date().isoformat()
        row = {
            "date": today,
            "followers_count": metrics.get("followers_count"),
            "following_count": metrics.get("following_count"),
            "tweet_count": metrics.get("tweet_count"),
            "listed_count": metrics.get("listed_count"),
        }
        resp = (
            self._c.table("analytics_daily")
            .upsert(row, on_conflict="date")
            .execute()
        )
        return resp.data[0] if resp.data else {}

    def upsert_tweet_metrics(
        self,
        tweet_id: str,
        *,
        content: str | None = None,
        likes: int = 0,
        retweets: int = 0,
        replies: int = 0,
        bookmarks: int = 0,
        impressions: int = 0,
    ) -> dict:
        row = {
            "tweet_id": str(tweet_id),
            "content": content,
            "likes": likes,
            "retweets": retweets,
            "replies": replies,
            "bookmarks": bookmarks,
            "impressions": impressions,
            "checked_at": _now_iso(),
        }
        resp = (
            self._c.table("tweet_metrics")
            .upsert(row, on_conflict="tweet_id")
            .execute()
        )
        return resp.data[0] if resp.data else {}

    # ── self-improving feedback: read what's working ─────────────────
    def get_performance_summary(
        self, *, min_posted: int = 10, lookback_days: int = 30
    ) -> dict:
        """Slice engagement by tone/pillar/hook_type/slot over posted tweets.

        Reads the `tweet_performance` view (engagement joined to dimensions).
        Returns {"ready": False, ...} until there's enough data to trust, so
        the generator's STEP 0 stays dormant at cold start. When impressions
        are missing (cheap API tier), ranks by weighted_engagement instead of
        engagement_rate.
        """
        cutoff = (datetime.now(_TZ) - timedelta(days=lookback_days)).isoformat()
        resp = (
            self._c.table("tweet_performance")
            .select("*")
            .gte("posted_at", cutoff)
            .execute()
        )
        rows = resp.data or []
        if len(rows) < min_posted:
            return {"ready": False, "reason": "cold-start", "n_posted": len(rows)}

        has_impressions = any((r.get("impressions") or 0) > 0 for r in rows)
        signal = "engagement_rate" if has_impressions else "weighted_engagement"

        def _agg(dimension: str) -> list[dict]:
            groups: dict[str, list[dict]] = {}
            for r in rows:
                key = r.get(dimension) or "unknown"
                groups.setdefault(key, []).append(r)
            out = []
            for key, items in groups.items():
                n = len(items)
                rates = [r["engagement_rate"] for r in items if r.get("engagement_rate") is not None]
                avg_rate = round(sum(rates) / len(rates), 4) if rates else None
                avg_weighted = round(
                    sum(r.get("weighted_engagement") or 0 for r in items) / n, 2
                )
                total_replies = sum(r.get("replies") or 0 for r in items)
                out.append(
                    {
                        "value": key,
                        "n": n,
                        "avg_engagement_rate": avg_rate,
                        "avg_weighted_engagement": avg_weighted,
                        "total_replies": total_replies,
                    }
                )
            sort_key = "avg_engagement_rate" if has_impressions else "avg_weighted_engagement"
            out.sort(key=lambda d: (d[sort_key] is not None, d[sort_key] or 0), reverse=True)
            return out

        return {
            "ready": True,
            "n_posted": len(rows),
            "signal": signal,
            "by_tone": _agg("tone"),
            "by_pillar": _agg("pillar"),
            "by_hook_type": _agg("hook_type"),
            "by_slot": _agg("slot"),
        }

    def get_follower_trend(self, lookback_days: int = 14) -> dict:
        """Follower delta over a window, from analytics_daily snapshots."""
        cutoff = (datetime.now(_TZ) - timedelta(days=lookback_days)).date().isoformat()
        resp = (
            self._c.table("analytics_daily")
            .select("date,followers_count")
            .gte("date", cutoff)
            .order("date")
            .execute()
        )
        rows = resp.data or []
        if len(rows) < 2:
            return {"ready": False, "n_days": len(rows)}
        start = rows[0].get("followers_count") or 0
        end = rows[-1].get("followers_count") or 0
        return {
            "ready": True,
            "start": start,
            "end": end,
            "delta": end - start,
            "days": len(rows),
        }

    # ── pause flag (shared between bot and scheduler) ────────────────
    def is_paused(self) -> bool:
        resp = (
            self._c.table("bot_state")
            .select("value")
            .eq("key", "paused")
            .limit(1)
            .execute()
        )
        if resp.data:
            return str(resp.data[0].get("value")).lower() == "true"
        return False

    def set_pause(self, paused: bool) -> None:
        self._c.table("bot_state").upsert(
            {"key": "paused", "value": "true" if paused else "false"},
            on_conflict="key",
        ).execute()


def create_db() -> Database:
    """Build a Database backed by a real Supabase service-role client."""
    from supabase import create_client

    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    client = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)
    return Database(client)
