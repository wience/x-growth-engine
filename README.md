# x-growth-engine

A personal X (Twitter) growth automation system for a single user. It does **not**
write content. Drafts are generated externally (a Littlebird Routine inserts them
straight into Supabase). This system's job:

1. **Store** drafts in Supabase (the single source of truth).
2. **Approve** them from your phone over Telegram.
3. **Post** approved ones on a schedule via the X API.
4. **Track** follower and engagement growth back into Supabase.

```
Littlebird Routine ──▶ Supabase tweet_queue ──▶ Telegram bot (approve/edit/skip)
                                  │
                                  ▼
                       Scheduler (Railway) ──▶ X API ──▶ Metrics back to Supabase
```

Drafts move through one state machine:

```
draft ──▶ approved ──▶ posting ──▶ posted
                   ╲              ╲
                    ▶ rejected     ▶ failed
```

## Design choices (made without asking, per the build brief)

- **Atomic claim via a Postgres function.** `claim_next_approved()` uses
  `FOR UPDATE SKIP LOCKED` to flip the next approved row to `posting` under a row
  lock *before* any API call. Two overlapping scheduler runs can never claim the
  same tweet, so double-posting is impossible. (A racy read-then-write in Python
  could not guarantee this.)
- **`bot_state` table** (one extra table beyond the original schema) holds the
  pause flag. The bot and scheduler are separate processes, so the flag has to
  live in the database, not memory.
- **Threads.** `claim_next_approved` only claims `single` and `thread_start` rows.
  When a `thread_start` is claimed, the scheduler gathers that parent's
  `thread_reply` children (ordered by `thread_index`) and posts them as one chain.
- **Metrics are best-effort.** Cheap X API tiers restrict *read* endpoints. Every
  metrics call is wrapped so a failure logs and moves on; it never crashes the
  scheduler or blocks posting. If `/stats` comes back empty, that's your tier.
- **tweepy**, not `xdk`. `supabase-py` v2. `python-telegram-bot` v21 (async).
  `APScheduler` `BlockingScheduler`. `zoneinfo`, not `pytz`.

## Layout

```
config.py              env + POSTING_SLOTS + DRY_RUN + TELEGRAM_USER_ID
src/x_client.py        tweepy wrapper: post_tweet, post_thread, get_my_metrics, get_tweet_metrics
src/db.py              Supabase wrapper + queue state machine
src/telegram_bot.py    /queue /pending /stats /pause /resume + approve/edit/skip buttons
src/scheduler.py       one job per posting slot + daily analytics; respects DRY_RUN
src/metrics.py         daily follower snapshot + engagement refresh (best-effort)
scripts/init_db.sql    schema + claim_next_approved() RPC
scripts/seed_from_json.py  manual fallback: load tweets.json into the queue
tests/                 queue transitions + X client retry/threading
```

## Setup

### 1. Credentials

Gather these first (you can't script their creation):

- **X API** — `developer.x.com` → Project + App, permissions **Read and Write**,
  generate API Key/Secret, Access Token/Secret, and a Bearer Token. **Check your
  actual access tier's read/write limits** — posting 3×/day is fine on Free, but
  the metrics features need read endpoints that Free largely doesn't allow.
- **Supabase** — a project dedicated to this bot. Grab the Project URL and the
  `service_role` key.
- **Telegram** — `@BotFather` → `/newbot` for the token; `@userinfobot` for your
  numeric user ID.

### 2. Environment

```bash
cp .env.example .env   # fill in every value
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Database

Open the Supabase SQL editor for your project and run the contents of
`scripts/init_db.sql`. This creates `tweet_queue`, `analytics_daily`,
`tweet_metrics`, `bot_state`, and the `claim_next_approved()` function.

### 4. Run locally

```bash
pytest                         # tests should pass

# terminal 1 — the control panel
python -m src.telegram_bot     # open Telegram, send /stats then /queue

# terminal 2 — the scheduler (DRY_RUN=true)
python -m src.scheduler        # prints the SGT schedule; logs would-post at slots
```

With `DRY_RUN=true` the scheduler logs the next approved tweet at each slot and
rolls the claim back to `approved` instead of posting. When `/stats` returns your
real follower count and the scheduler prints the schedule without posting, the
wiring is correct.

No drafts yet? Seed some manually:

```bash
python -m scripts.seed_from_json tweets.json
```

## Wiring Littlebird to feed the queue

1. In Littlebird → Settings → Integrations → Supabase, connect it to this bot's
   Supabase project. The credential lives in Littlebird's integration store.
2. Create a weekly Routine that, after generating its JSON, inserts each surviving
   tweet into `tweet_queue` with `status='draft'`, `source='routine'`, and the
   `pillar` / `grounded_in` / `slot` / `format` columns set. For a thread, insert
   the parent as `thread_start` first, then each body as `thread_reply` with
   `thread_parent_id` and an incrementing `thread_index`.

You review drafts on Telegram; the scheduler posts the approved ones.

## Deploy to Railway

```bash
git add . && git commit -m "x growth engine"
# push to a private GitHub repo, then on railway.app:
#   New Project → Deploy from GitHub repo
#   Variables tab → add every key from .env
```

Run two processes (the `Procfile` defines both):

- `worker: python -m src.scheduler`
- `bot: python -m src.telegram_bot`

On Railway, the `worker` runs automatically; add the `bot` as a second service (or
second process) pointed at the same repo and env vars.

**Leave `DRY_RUN=true` for the first day.** Watch one real slot fire in the logs,
confirm it would post the right tweet, then set `DRY_RUN=false` in the Variables
tab and redeploy.

## Telegram commands

| Command | Does |
|---------|------|
| `/start` | Help |
| `/queue` | Up to 5 drafts, each with ✅ Approve / ✏️ Edit / ❌ Skip |
| `/pending` | Counts: drafts, approved, posted today |
| `/stats` | Current follower/following/tweet counts (best-effort) |
| `/pause` | Scheduler skips posting |
| `/resume` | Posting re-enabled |

Approve → `approved`. Skip → `rejected`. Edit → bot asks for replacement text and
updates the draft. **Nothing posts from the bot** — the scheduler owns posting.

## Notes / honest limits

- The system buys consistency, not reach. Followers come from replying to people
  in your niche; keep 15 minutes a day for that.
- The `grounded_in` field is your human gate. If you don't recognise what a draft
  traces to, skip it. Nothing posts under your name without you seeing it.
- This repo is public; it contains **no secrets** — every credential is an env var
  and `.env` is gitignored. Don't commit a real `.env`.
