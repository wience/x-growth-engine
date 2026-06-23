-- X growth engine schema. Run once in the Supabase SQL editor.

create extension if not exists "pgcrypto";

-- ── tweet_queue: the single source of truth for drafts and their lifecycle ──
create table if not exists tweet_queue (
  id                uuid primary key default gen_random_uuid(),
  content           text not null,
  thread_parent_id  uuid references tweet_queue(id),
  thread_index      int default 0,
  format            text default 'single',   -- single | thread_start | thread_reply
  pillar            text,
  grounded_in       text,
  slot              text,                     -- morning | lunch | evening
  status            text default 'draft',     -- draft | approved | posting | posted | failed | rejected
  source            text default 'routine',
  scheduled_at      timestamptz,
  posted_at         timestamptz,
  tweet_id          text,
  error             text,
  created_at        timestamptz default now()
);

create index if not exists tweet_queue_status_idx on tweet_queue (status, created_at);
create index if not exists tweet_queue_parent_idx on tweet_queue (thread_parent_id, thread_index);

-- ── analytics_daily: one follower/account snapshot per day ──────────────────
create table if not exists analytics_daily (
  id               uuid primary key default gen_random_uuid(),
  date             date unique not null,
  followers_count  int,
  following_count  int,
  tweet_count      int,
  listed_count     int,
  created_at       timestamptz default now()
);

-- ── tweet_metrics: engagement per posted tweet, refreshed over time ─────────
create table if not exists tweet_metrics (
  id           uuid primary key default gen_random_uuid(),
  tweet_id     text unique not null,
  content      text,
  likes        int default 0,
  retweets     int default 0,
  replies      int default 0,
  bookmarks    int default 0,
  impressions  int default 0,
  checked_at   timestamptz default now()
);

-- ── bot_state: tiny key/value store shared between bot and scheduler ────────
-- (added beyond the original spec so /pause works across separate processes)
create table if not exists bot_state (
  key    text primary key,
  value  text
);
insert into bot_state (key, value) values ('paused', 'false')
  on conflict (key) do nothing;

-- ── claim_next_approved(): atomic claim so overlapping runs never double-post ──
-- Picks the oldest approved single/thread_start row, flips it to 'posting'
-- under a row lock, and returns it. SKIP LOCKED means a second concurrent
-- caller grabs the next row instead of blocking or grabbing the same one.
create or replace function claim_next_approved()
returns setof tweet_queue
language plpgsql
as $$
declare
  claimed tweet_queue;
begin
  select * into claimed
  from tweet_queue
  where status = 'approved'
    and format in ('single', 'thread_start')
  order by created_at
  for update skip locked
  limit 1;

  if not found then
    return;
  end if;

  update tweet_queue
  set status = 'posting'
  where id = claimed.id
  returning * into claimed;

  return next claimed;
end;
$$;
