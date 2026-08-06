"""Telegram bot command handlers."""
from sqlmodel import Session, select
from telegram import Update
from telegram.ext import ContextTypes

from app.db import engine
from app.models import AppUser
from app.services.telegram_link import link_telegram


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message
    # Only ever link a 1:1 private chat. Linking a group/channel would broadcast
    # the user's personal notifications to everyone in it; channel posts also
    # carry no `message`, which would crash the old update.message access.
    if chat is None or chat.type != "private" or message is None:
        return
    chat_id = chat.id
    args = context.args or []

    with Session(engine) as session:
        user = link_telegram(session, args[0], chat_id) if args else None
        if user is not None:
            await message.reply_text(f"✅ Connected as {user.real_name}.")
            return
        # No token, or it's invalid/expired. If this chat is already linked
        # (e.g. a second /start), say so instead of a scary error.
        already = session.exec(
            select(AppUser).where(AppUser.telegram_id == str(chat_id))
        ).first()

    if already is not None:
        await message.reply_text(f"✅ Already connected as {already.real_name}.")
    elif args:
        await message.reply_text("❌ Invalid or expired link. Open the portal and try again.")
    else:
        await message.reply_text(
            "Open the portal and use 'Connect Telegram' to link your account."
        )
