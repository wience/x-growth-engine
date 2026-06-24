"""Telegram control panel (runs on your phone).

Commands: /start /queue /pending /stats /pause /resume
The bot NEVER posts. It only moves drafts through statuses; the scheduler posts.

Auth: every update is rejected unless effective_user.id == TELEGRAM_USER_ID.
"""
import functools
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from src.db import Database, create_db
from src.x_client import XClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("telegram_bot")

# Stashed on the Application via bot_data so handlers can reach them.
DB_KEY = "db"
X_KEY = "x"
EDITING_KEY = "editing_id"  # per-user context.user_data


def restricted(handler):
    """Reject any update not from the configured owner."""

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id != config.TELEGRAM_USER_ID:
            log.warning("Rejected update from user id=%s", getattr(user, "id", None))
            if update.effective_message:
                await update.effective_message.reply_text("Not authorised.")
            return
        return await handler(update, context)

    return wrapper


def _db(context) -> Database:
    return context.application.bot_data[DB_KEY]


def _x(context) -> XClient:
    return context.application.bot_data[X_KEY]


def _draft_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{draft_id}"),
                InlineKeyboardButton("✏️ Edit", callback_data=f"edit:{draft_id}"),
                InlineKeyboardButton("❌ Skip", callback_data=f"skip:{draft_id}"),
            ]
        ]
    )


@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "X growth engine control panel.\n\n"
        "/queue — review up to 5 drafts (Approve / Edit / Skip)\n"
        "/pending — counts of drafts, approved, posted today\n"
        "/stats — current account metrics\n"
        "/insights — what's converting (once you've posted enough)\n"
        "/pause — stop the scheduler from posting\n"
        "/resume — allow posting again"
    )


@restricted
async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    drafts = _db(context).get_drafts("draft", limit=5)
    if not drafts:
        await update.message.reply_text("No drafts waiting. 🎉")
        return
    for d in drafts:
        header = []
        if d.get("pillar"):
            header.append(f"#{d['pillar']}")
        if d.get("format") and d["format"] != "single":
            header.append(d["format"])
        prefix = (" ".join(header) + "\n") if header else ""
        grounded = f"\n↳ grounded in: {d['grounded_in']}" if d.get("grounded_in") else ""
        await update.message.reply_text(
            f"{prefix}{d['content']}{grounded}",
            reply_markup=_draft_keyboard(d["id"]),
        )


@restricted
async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    c = _db(context).counts_today()
    await update.message.reply_text(
        f"drafts: {c['draft']}\napproved: {c['approved']}\nposted today: {c['posted_today']}"
    )


@restricted
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        m = _x(context).get_my_metrics()
    except Exception as exc:  # noqa: BLE001 — read endpoints may be tier-restricted
        log.warning("get_my_metrics failed: %s", exc)
        await update.message.reply_text(
            "Couldn't fetch metrics — your X API tier may not allow reads."
        )
        return
    if m.get("followers_count") is None:
        await update.message.reply_text(
            "Metrics came back empty — your X API tier may not allow reads."
        )
        return
    handle = f"@{m['username']}\n" if m.get("username") else ""
    await update.message.reply_text(
        f"{handle}followers: {m['followers_count']}\n"
        f"following: {m['following_count']}\n"
        f"tweets: {m['tweet_count']}\n"
        f"listed: {m['listed_count']}"
    )


@restricted
async def cmd_insights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """What's converting, sliced by tone/pillar/hook. Dormant until enough posts."""
    db = _db(context)
    summary = db.get_performance_summary()
    trend = db.get_follower_trend()

    lines = []
    if trend.get("ready"):
        d = trend["delta"]
        sign = "+" if d >= 0 else ""
        lines.append(f"followers: {trend['end']} ({sign}{d} over {trend['days']}d)")

    if not summary.get("ready"):
        lines.append(
            f"cold-start: not enough data yet ({summary.get('n_posted', 0)} posted). "
            "Need ~10 posted tweets before insights mean anything."
        )
        await update.message.reply_text("\n".join(lines))
        return

    lines.append(f"\nbased on {summary['n_posted']} posts, ranked by {summary['signal']}:")
    metric_key = (
        "avg_engagement_rate"
        if summary["signal"] == "engagement_rate"
        else "avg_weighted_engagement"
    )
    for label, dim in (("tone", "by_tone"), ("pillar", "by_pillar"), ("hook", "by_hook_type")):
        top = summary[dim][:3]
        parts = [f"{r['value']} ({r[metric_key]}, n={r['n']})" for r in top]
        lines.append(f"\n{label}: " + " · ".join(parts))
    await update.message.reply_text("\n".join(lines))


@restricted
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _db(context).set_pause(True)
    await update.message.reply_text("⏸ Paused. The scheduler will skip posting.")


@restricted
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _db(context).set_pause(False)
    await update.message.reply_text("▶️ Resumed. Posting re-enabled.")


@restricted
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, _, draft_id = query.data.partition(":")
    db = _db(context)

    if action == "approve":
        db.set_status(draft_id, "approved")
        await query.edit_message_text(f"✅ Approved.\n\n{query.message.text}")
    elif action == "skip":
        db.set_status(draft_id, "rejected")
        await query.edit_message_text(f"❌ Skipped.\n\n{query.message.text}")
    elif action == "edit":
        context.user_data[EDITING_KEY] = draft_id
        await query.message.reply_text("Send the replacement text for this draft.")


@restricted
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Capture replacement text after an Edit tap."""
    draft_id = context.user_data.pop(EDITING_KEY, None)
    if not draft_id:
        return  # not in an edit flow; ignore
    new_text = update.message.text
    _db(context).update_content(draft_id, new_text)
    await update.message.reply_text(
        "✏️ Updated. Send /queue to review it again.",
    )


def build_app(db: Database, x: XClient) -> Application:
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN must be set")
    if not config.TELEGRAM_USER_ID:
        raise RuntimeError("TELEGRAM_USER_ID must be set")

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.bot_data[DB_KEY] = db
    app.bot_data[X_KEY] = x

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("insights", cmd_insights))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


def main() -> None:
    log.info("Starting Telegram bot.")
    app = build_app(create_db(), XClient())
    app.run_polling()


if __name__ == "__main__":
    main()
