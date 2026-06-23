"""X client: thread chaining + retry/backoff, with tweepy fully mocked."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import tweepy

from src.x_client import XClient


def resp(tweet_id):
    return SimpleNamespace(data={"id": tweet_id})


def make_exc(cls):
    """Build a tweepy exception without running its requests-dependent init."""
    return cls.__new__(cls)


@pytest.fixture
def fake():
    return MagicMock()


@pytest.fixture
def x(fake):
    # sleep is a no-op so backoff doesn't slow tests.
    return XClient(client=fake, sleep=lambda *_: None)


def test_post_tweet_returns_string_id(fake, x):
    fake.create_tweet.return_value = resp(999)
    assert x.post_tweet("hello") == "999"
    fake.create_tweet.assert_called_once_with(text="hello", in_reply_to_tweet_id=None)


def test_post_thread_chains_replies(fake, x):
    fake.create_tweet.side_effect = [resp(1), resp(2), resp(3)]
    ids = x.post_thread(["a", "b", "c"])
    assert ids == ["1", "2", "3"]
    calls = fake.create_tweet.call_args_list
    assert calls[0].kwargs["in_reply_to_tweet_id"] is None
    assert calls[1].kwargs["in_reply_to_tweet_id"] == "1"
    assert calls[2].kwargs["in_reply_to_tweet_id"] == "2"


def test_retry_then_success_on_rate_limit(fake, x):
    fake.create_tweet.side_effect = [make_exc(tweepy.TooManyRequests), resp(5)]
    assert x.post_tweet("hi") == "5"
    assert fake.create_tweet.call_count == 2


def test_retry_on_server_error(fake, x):
    fake.create_tweet.side_effect = [make_exc(tweepy.TwitterServerError), resp(7)]
    assert x.post_tweet("hi") == "7"
    assert fake.create_tweet.call_count == 2


def test_retry_gives_up_after_max_tries(fake, x):
    fake.create_tweet.side_effect = [make_exc(tweepy.TooManyRequests)] * 3
    with pytest.raises(tweepy.TooManyRequests):
        x.post_tweet("hi")
    assert fake.create_tweet.call_count == 3


def test_non_retryable_error_raises_immediately(fake, x):
    fake.create_tweet.side_effect = ValueError("bad")
    with pytest.raises(ValueError):
        x.post_tweet("hi")
    assert fake.create_tweet.call_count == 1


def test_get_my_metrics_maps_public_metrics(fake, x):
    user = SimpleNamespace(
        username="bob",
        public_metrics={
            "followers_count": 10,
            "following_count": 5,
            "tweet_count": 3,
            "listed_count": 1,
        },
    )
    fake.get_me.return_value = SimpleNamespace(data=user)
    m = x.get_my_metrics()
    assert m["username"] == "bob"
    assert m["followers_count"] == 10
    assert m["listed_count"] == 1


def test_get_tweet_metrics_prefers_non_public_impressions(fake, x):
    tweet = SimpleNamespace(
        public_metrics={
            "like_count": 2,
            "retweet_count": 1,
            "reply_count": 0,
            "bookmark_count": 4,
        },
        non_public_metrics={"impression_count": 100},
    )
    fake.get_tweet.return_value = SimpleNamespace(data=tweet)
    m = x.get_tweet_metrics("9")
    assert m["likes"] == 2
    assert m["retweets"] == 1
    assert m["bookmarks"] == 4
    assert m["impressions"] == 100
