"""Thin tweepy wrapper for X (Twitter) API v2.

Only the operations this bot needs: post a single tweet, post a thread (chained
via in_reply_to_tweet_id), read account metrics, read a tweet's metrics.

Retries on rate limit (429) and server errors (5xx) with exponential backoff.

NOTE on tweepy: methods match tweepy 4.x's Client (create_tweet, get_me,
get_tweet). If a future tweepy changes these signatures, this is the single
file to adapt.
"""
import logging
import time

import tweepy

import config

log = logging.getLogger(__name__)

MAX_TRIES = 3
# Exceptions worth retrying: rate limit and transient server errors.
RETRYABLE = (tweepy.TooManyRequests, tweepy.TwitterServerError)


def _with_retry(fn, *args, _sleep=time.sleep, **kwargs):
    """Call fn, retrying on 429/5xx with exponential backoff (2s, 4s)."""
    last_exc = None
    for attempt in range(MAX_TRIES):
        try:
            return fn(*args, **kwargs)
        except RETRYABLE as exc:
            last_exc = exc
            if attempt == MAX_TRIES - 1:
                break
            wait = 2 ** (attempt + 1)
            log.warning(
                "X API %s (attempt %d/%d); retrying in %ds",
                type(exc).__name__, attempt + 1, MAX_TRIES, wait,
            )
            _sleep(wait)
    raise last_exc


class XClient:
    def __init__(self, client: tweepy.Client | None = None, sleep=time.sleep):
        self._client = client or tweepy.Client(
            bearer_token=config.X_BEARER_TOKEN or None,
            consumer_key=config.X_API_KEY,
            consumer_secret=config.X_API_SECRET,
            access_token=config.X_ACCESS_TOKEN,
            access_token_secret=config.X_ACCESS_TOKEN_SECRET,
        )
        self._sleep = sleep

    # ── writes ──────────────────────────────────────────────────────
    def post_tweet(self, text: str, in_reply_to_tweet_id: str | None = None) -> str:
        """Post one tweet, return its id (as a string)."""
        resp = _with_retry(
            self._client.create_tweet,
            text=text,
            in_reply_to_tweet_id=in_reply_to_tweet_id,
            _sleep=self._sleep,
        )
        tweet_id = str(resp.data["id"])
        log.info("Posted tweet %s", tweet_id)
        return tweet_id

    def post_thread(self, texts: list[str]) -> list[str]:
        """Post a list of tweets as a chained thread; return ids in order.

        Each tweet after the first replies to the previous one.
        """
        ids: list[str] = []
        reply_to: str | None = None
        for text in texts:
            tweet_id = self.post_tweet(text, in_reply_to_tweet_id=reply_to)
            ids.append(tweet_id)
            reply_to = tweet_id
        return ids

    # ── reads (best-effort; cheap API tiers may restrict these) ──────
    def get_my_metrics(self) -> dict:
        resp = _with_retry(
            self._client.get_me,
            user_fields=["public_metrics"],
            _sleep=self._sleep,
        )
        user = resp.data
        pm = (user.public_metrics or {}) if user else {}
        return {
            "username": getattr(user, "username", None),
            "followers_count": pm.get("followers_count"),
            "following_count": pm.get("following_count"),
            "tweet_count": pm.get("tweet_count"),
            "listed_count": pm.get("listed_count"),
        }

    def get_tweet_metrics(self, tweet_id: str) -> dict:
        resp = _with_retry(
            self._client.get_tweet,
            tweet_id,
            tweet_fields=["public_metrics", "non_public_metrics"],
            _sleep=self._sleep,
        )
        tweet = resp.data
        pm = (tweet.public_metrics or {}) if tweet else {}
        npm = (getattr(tweet, "non_public_metrics", None) or {}) if tweet else {}
        return {
            "likes": pm.get("like_count", 0),
            "retweets": pm.get("retweet_count", 0),
            "replies": pm.get("reply_count", 0),
            "bookmarks": pm.get("bookmark_count", 0),
            "impressions": npm.get("impression_count")
            or pm.get("impression_count", 0),
        }
