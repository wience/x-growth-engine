"""Manual fallback loader: insert a local tweets.json array into tweet_queue
as drafts. Use this when Littlebird isn't wired up yet, or to backfill.

tweets.json format — an array of objects:
[
  {"content": "single tweet text", "pillar": "build-in-public", "slot": "morning"},
  {
    "format": "thread",
    "pillar": "lessons",
    "tweets": ["first tweet (the hook)", "second", "third"]
  }
]

A "thread" object is expanded into a thread_start row plus thread_reply rows
linked by thread_parent_id and ordered by thread_index.

Run:  python -m scripts.seed_from_json [path/to/tweets.json]
"""
import json
import logging
import sys

from src.db import create_db

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("seed")


def load(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    if not isinstance(items, list):
        raise ValueError("tweets.json must be a JSON array")

    db = create_db()
    inserted = 0

    for item in items:
        common = {
            "pillar": item.get("pillar"),
            "grounded_in": item.get("grounded_in"),
            "slot": item.get("slot"),
            "source": item.get("source", "manual"),
        }

        if item.get("format") == "thread" or item.get("tweets"):
            tweets = item.get("tweets") or []
            if not tweets:
                log.warning("Thread item with no tweets — skipping.")
                continue
            parent = db.add_draft(
                tweets[0], format="thread_start", thread_index=0, **common
            )
            inserted += 1
            for i, body in enumerate(tweets[1:], start=1):
                db.add_draft(
                    body,
                    format="thread_reply",
                    thread_parent_id=parent["id"],
                    thread_index=i,
                    **common,
                )
                inserted += 1
        else:
            db.add_draft(item["content"], format="single", **common)
            inserted += 1

    log.info("Inserted %d rows from %s", inserted, path)
    return inserted


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "tweets.json"
    load(path)
