"""Queue state-transition logic, tested against a fake Supabase client.

The fake records the last query so we can assert what the wrapper sent, and
returns whatever data/count the test programs.
"""
import pytest

from src.db import Database


class FakeResponse:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class FakeQuery:
    def __init__(self, client, table=None):
        self.client = client
        self.table_name = table
        self.op = None
        self.payload = None
        self.on_conflict = None
        self.eqs = {}
        self.order_by = None
        self.limit_n = None
        self.rpc_name = None
        self.rpc_params = None

    def insert(self, row):
        self.op = "insert"
        self.payload = row
        return self

    def update(self, row):
        self.op = "update"
        self.payload = row
        return self

    def upsert(self, row, on_conflict=None):
        self.op = "upsert"
        self.payload = row
        self.on_conflict = on_conflict
        return self

    def select(self, *args, count=None):
        self.op = self.op or "select"
        self.select_args = args
        self.select_count = count
        return self

    def eq(self, key, value):
        self.eqs[key] = value
        return self

    def gte(self, key, value):
        self.eqs[f"gte:{key}"] = value
        return self

    def order(self, col, desc=False):
        self.order_by = (col, desc)
        return self

    def limit(self, n):
        self.limit_n = n
        return self

    @property
    def not_(self):
        return self

    def is_(self, key, value):
        return self

    def execute(self):
        self.client.last = self
        return FakeResponse(self.client.next_data, self.client.next_count)


class FakeClient:
    def __init__(self):
        self.next_data = []
        self.next_count = None
        self.last = None

    def table(self, name):
        return FakeQuery(self, name)

    def rpc(self, name, params=None):
        q = FakeQuery(self)
        q.rpc_name = name
        q.rpc_params = params
        return q


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def db(client):
    return Database(client)


def test_add_draft_defaults_to_draft_status(client, db):
    client.next_data = [{"id": "1", "content": "hi", "status": "draft"}]
    row = db.add_draft("hi", pillar="build")
    assert client.last.op == "insert"
    assert client.last.table_name == "tweet_queue"
    assert client.last.payload["status"] == "draft"
    assert client.last.payload["content"] == "hi"
    assert client.last.payload["pillar"] == "build"
    assert row["id"] == "1"


def test_set_status_updates_only_status(client, db):
    client.next_data = [{"id": "1", "status": "approved"}]
    db.set_status("1", "approved")
    assert client.last.op == "update"
    assert client.last.payload == {"status": "approved"}
    assert client.last.eqs["id"] == "1"


def test_get_drafts_filters_status_and_limit(client, db):
    client.next_data = [{"id": "1"}]
    db.get_drafts("draft", limit=3)
    assert client.last.eqs["status"] == "draft"
    assert client.last.limit_n == 3
    assert client.last.order_by[0] == "created_at"


def test_mark_posted_sets_terminal_fields(client, db):
    client.next_data = [{"id": "1"}]
    db.mark_posted("1", "12345")
    p = client.last.payload
    assert p["status"] == "posted"
    assert p["tweet_id"] == "12345"
    assert "posted_at" in p
    assert client.last.eqs["id"] == "1"


def test_mark_failed_records_error(client, db):
    client.next_data = [{"id": "1"}]
    db.mark_failed("1", "boom")
    assert client.last.payload["status"] == "failed"
    assert client.last.payload["error"] == "boom"


def test_claim_next_approved_uses_rpc(client, db):
    client.next_data = [{"id": "1", "format": "single"}]
    row = db.claim_next_approved()
    assert client.last.rpc_name == "claim_next_approved"
    assert row["id"] == "1"


def test_claim_next_approved_returns_none_when_empty(client, db):
    client.next_data = []
    assert db.claim_next_approved() is None


def test_save_daily_analytics_upserts_on_date(client, db):
    client.next_data = [{"date": "2026-06-24"}]
    db.save_daily_analytics({"followers_count": 100, "following_count": 50})
    assert client.last.op == "upsert"
    assert client.last.on_conflict == "date"
    assert client.last.payload["followers_count"] == 100


def test_pause_flag_roundtrip(client, db):
    db.set_pause(True)
    assert client.last.op == "upsert"
    assert client.last.payload == {"key": "paused", "value": "true"}

    client.next_data = [{"value": "true"}]
    assert db.is_paused() is True

    client.next_data = []
    assert db.is_paused() is False


# ── feedback-loop dimensions ─────────────────────────────────────────
def test_add_draft_persists_tone_hook_score(client, db):
    client.next_data = [{"id": "1"}]
    db.add_draft("hi", tone="humor", hook_type="relatable confession", score=27)
    p = client.last.payload
    assert p["tone"] == "humor"
    assert p["hook_type"] == "relatable confession"
    assert p["score"] == 27


def test_get_recent_posted_carries_dimensions(client, db):
    client.next_data = [{"id": "1"}]
    db.get_recent_posted()
    cols = client.last.select_args[0]
    for c in ("pillar", "tone", "hook_type", "slot", "format", "score"):
        assert c in cols


def test_performance_summary_cold_start(client, db):
    client.next_data = [{"tweet_id": str(i)} for i in range(3)]  # < min_posted
    out = db.get_performance_summary(min_posted=10)
    assert out["ready"] is False
    assert out["reason"] == "cold-start"
    assert out["n_posted"] == 3


def test_performance_summary_ranks_by_rate_when_impressions_exist(client, db):
    # humor outperforms technical on engagement_rate
    rows = []
    for i in range(6):
        rows.append({"tone": "humor", "pillar": "p", "hook_type": "h", "slot": "morning",
                     "impressions": 100, "engagement_rate": 0.10, "weighted_engagement": 10, "replies": 1})
    for i in range(6):
        rows.append({"tone": "technical", "pillar": "p", "hook_type": "h", "slot": "lunch",
                     "impressions": 100, "engagement_rate": 0.02, "weighted_engagement": 2, "replies": 0})
    client.next_data = rows
    out = db.get_performance_summary(min_posted=10)
    assert out["ready"] is True
    assert out["signal"] == "engagement_rate"
    assert out["by_tone"][0]["value"] == "humor"  # highest rate ranks first


def test_performance_summary_falls_back_without_impressions(client, db):
    rows = [
        {"tone": "humor", "pillar": "p", "hook_type": "h", "slot": "morning",
         "impressions": 0, "engagement_rate": None, "weighted_engagement": 8, "replies": 2}
        for _ in range(12)
    ]
    client.next_data = rows
    out = db.get_performance_summary(min_posted=10)
    assert out["signal"] == "weighted_engagement"


def test_follower_trend(client, db):
    client.next_data = []
    assert db.get_follower_trend()["ready"] is False

    client.next_data = [
        {"date": "2026-06-01", "followers_count": 10},
        {"date": "2026-06-14", "followers_count": 25},
    ]
    out = db.get_follower_trend()
    assert out["ready"] is True
    assert out["delta"] == 15
