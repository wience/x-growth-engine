"""APScheduler process. Posts the next approved tweet at each slot and takes a
daily analytics snapshot. Respects DRY_RUN and the shared pause flag.

This process OWNS posting. The Telegram bot only changes draft status.
"""
import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import config
from src import metrics as metrics_mod
from src.db import Database, create_db
from src.x_client import XClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("scheduler")


def post_next_approved(db: Database, x: XClient, slot: str | None = None) -> None:
    """Claim and post the next approved tweet. Idempotent: the claim flips
    status to 'posting' before any API call, so overlapping runs are safe."""
    if db.is_paused():
        log.info("[%s] Paused — skipping.", slot or "slot")
        return

    row = db.claim_next_approved()
    if not row:
        log.info("[%s] Nothing approved to post.", slot or "slot")
        return

    draft_id = row["id"]
    fmt = row.get("format", "single")

    # Build the list of texts to post (single tweet or a thread chain).
    if fmt == "thread_start":
        children = db.get_thread_children(draft_id)
        texts = [row["content"]] + [c["content"] for c in children]
        child_ids = [c["id"] for c in children]
    else:
        texts = [row["content"]]
        child_ids = []

    if config.DRY_RUN:
        log.info(
            "[DRY_RUN][%s] Would post %d tweet(s); first: %r",
            slot or "slot", len(texts), texts[0][:80],
        )
        # Roll the claim back so it isn't stuck in 'posting' during dry runs.
        db.set_status(draft_id, "approved")
        for cid in child_ids:
            db.set_status(cid, "approved")
        return

    try:
        if len(texts) > 1:
            ids = x.post_thread(texts)
            db.mark_posted(draft_id, ids[0])
            for cid, tid in zip(child_ids, ids[1:]):
                db.mark_posted(cid, tid)
        else:
            tweet_id = x.post_tweet(texts[0])
            db.mark_posted(draft_id, tweet_id)
        log.info("[%s] Posted draft %s.", slot or "slot", draft_id)
    except Exception as exc:  # noqa: BLE001 — log, mark failed, keep running
        log.exception("[%s] Failed to post draft %s: %s", slot or "slot", draft_id, exc)
        db.mark_failed(draft_id, str(exc))
        for cid in child_ids:
            db.mark_failed(cid, str(exc))


def daily_analytics(db: Database, x: XClient) -> None:
    """Best-effort: never let a metrics failure crash the scheduler."""
    metrics_mod.run_daily(db, x)


def build_scheduler(db: Database, x: XClient) -> BlockingScheduler:
    sched = BlockingScheduler(timezone=config.TIMEZONE)

    jitter = config.POSTING_JITTER_MINUTES * 60
    for label, hhmm in config.POSTING_SLOTS:
        hour, minute = (int(p) for p in hhmm.split(":"))
        sched.add_job(
            post_next_approved,
            CronTrigger(hour=hour, minute=minute, jitter=jitter),
            kwargs={"db": db, "x": x, "slot": label},
            id=f"slot_{label}",
            name=f"post::{label}",
            max_instances=1,
            coalesce=True,
        )
        log.info(
            "Scheduled slot '%s' at %s (+0-%dm jitter) %s",
            label, hhmm, config.POSTING_JITTER_MINUTES, config.TIMEZONE,
        )

    a_hour, a_minute = (int(p) for p in config.ANALYTICS_TIME.split(":"))
    sched.add_job(
        daily_analytics,
        CronTrigger(hour=a_hour, minute=a_minute),
        kwargs={"db": db, "x": x},
        id="daily_analytics",
        name="analytics::daily",
        max_instances=1,
        coalesce=True,
    )
    log.info("Scheduled daily analytics at %s %s", config.ANALYTICS_TIME, config.TIMEZONE)
    return sched


def main() -> None:
    log.info("Starting scheduler. DRY_RUN=%s TIMEZONE=%s", config.DRY_RUN, config.TIMEZONE)
    db = create_db()
    x = XClient()
    sched = build_scheduler(db, x)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
