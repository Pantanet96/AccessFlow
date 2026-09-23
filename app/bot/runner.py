"""Start/stop the python-telegram-bot Application as an asyncio task."""
import logging

from telegram.ext import Application, CommandHandler

from app import runtime_config
from app.bot.handlers import start_command


async def start_bot() -> Application:
    application = (
        Application.builder().token(runtime_config.telegram_config()["token"]).build()
    )
    application.add_handler(CommandHandler("start", start_command))
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    return application


async def stop_bot(application: Application) -> None:
    if application.updater is not None:
        await application.updater.stop()
    await application.stop()
    await application.shutdown()


async def restart_bot(app) -> None:
    """(Re)start polling with the current token, stopping the previous bot.
    Called at startup and when the token is changed from settings: without it
    the old token kept polling (link /start broke) until a container restart."""
    old = app.state.telegram_bot
    app.state.telegram_bot = None
    if old is not None:
        await stop_bot(old)
    if not runtime_config.telegram_config()["token"]:
        return
    try:
        app.state.telegram_bot = await start_bot()
    except Exception:
        logging.getLogger(__name__).warning(
            "Telegram bot failed to start (bad token or network) — "
            "continuing without it.", exc_info=True,
        )
