"""Start/stop the python-telegram-bot Application as an asyncio task."""
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
