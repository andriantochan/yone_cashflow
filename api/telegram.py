from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)

from main import (
    start,
    add,
    list_tx,
    show_summary,
    free_entry,
    ocr_photo,
    free_desc,
    free_amount,
    free_txdate,
    free_type,
    free_bank,
    free_category,
    free_cancel,
    DESC,
    AMOUNT,
    TXDATE,
    TYPE,
    BANK,
    CATEGORY,
    TELEGRAM_BOT_TOKEN,
)

app = FastAPI()

# Build PTB application once at cold start
application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

conv = ConversationHandler(
    entry_points=[
        MessageHandler(filters.TEXT & ~filters.COMMAND, free_entry),
        MessageHandler(filters.PHOTO, ocr_photo),
    ],
    states={
        DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, free_desc)],
        AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, free_amount)],
        TXDATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, free_txdate)],
        TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, free_type)],
        BANK: [MessageHandler(filters.TEXT & ~filters.COMMAND, free_bank)],
        CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, free_category)],
    },
    fallbacks=[CommandHandler("cancel", free_cancel)],
)

application.add_handler(conv)
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("add", add))
application.add_handler(CommandHandler("list", list_tx))
application.add_handler(CommandHandler("summary", show_summary))


@app.post("/")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}


