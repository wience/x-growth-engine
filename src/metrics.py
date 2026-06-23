"""Daily follower snapshot + engagement refresh for recently posted tweets.

Everything here is BEST-EFFORT. Cheap X API tiers restrict read endpoints, so a
failure must log and move on, never crash the scheduler.
"""
import logging

import config
from src.db import Database
from src.x_client import XClient

log = logging.getLogger(__name__)


def snapshot_followers(db: Database, x: XClient) -> None:
    try:
        metrics = x.get_my_metrics()
        if metrics.get("followers_count") is None:
            log.warning("Follower metrics unavailable (likely API tier); skipping snapshot.")
            return
        db.save_daily_analytics(metrics)
        log.info("Saved daily analytics: %s followers", metrics.get("followers_count"))
    except Exception as exc:  # noqa: BLE001
        log.warning("Follower snapshot failed (best-effort): %s", exc)


def refresh_recent_engagement(db: Database, x: XClient) -> None:
    try:
        posted = db.get_recent_posted(lookback_days=config.METRICS_LOOKBACK_DAYS)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not load recent posted tweets: %s", exc)
        return

    refreshed = 0
    for row in posted:
        tweet_id = row.get("tweet_id")
        if not tweet_id:
            continue
        try:
            m = x.get_tweet_metrics(tweet_id)
            db.upsert_tweet_metrics(
                tweet_id,
                content=row.get("content"),
                likes=m["likes"],
                retweets=m["retweets"],
                replies=m["replies"],
                bookmarks=m["bookmarks"],
                impressions=m["impressions"],
            )
            refreshed += 1
        except Exception as exc:  # noqa: BLE001 — one tweet failing shouldn't stop the rest
            log.warning("Engagement refresh failed for %s (best-effort): %s", tweet_id, exc)
    log.info("Refreshed engagement for %d/%d tweets.", refreshed, len(posted))


def run_daily(db: Database, x: XClient) -> None:
    snapshot_followers(db, x)
    refresh_recent_engagement(db, x)
