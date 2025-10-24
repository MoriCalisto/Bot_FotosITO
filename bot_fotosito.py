# -*- coding: utf-8 -*-
"""
Bot de Telegram (Render friendly)
- Pregunta única: BR-OR, BR-PON, TALL-OR, TALL-PON, LOE-OR, LOE-PON
- Guarda en subcarpetas y registra CSV
- Incluye microservidor HTTP (aiohttp) para satisfacer el healthcheck de Render
"""

import os
import asyncio
import signal
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    CallbackQueryHandler, ContextTypes, filters
)

from aiohttp import web  # micro web server para Render

# ================= CONFIG =================
TOKEN = os.getenv("BOT_TOKEN", "").strip()                 # Render: Environment → BOT_TOKEN
PHOTO_SAVE_ROOT = os.getenv("PHOTO_SAVE_ROOT", "./photos") # En Render es un FS efímero (sirve para procesar), OneDrive se puede reactivar luego
PRINCIPAL_CHOICES = ["BR-OR", "BR-PON", "TALL-OR", "TALL-PON", "LOE-OR", "LOE-PON"]
CSV_LOG = os.path.join(PHOTO_SAVE_ROOT, "registro_fotos.csv")
ASK_PRINCIPAL = 0

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("BotFotosITO")

os.makedirs(PHOTO_SAVE_ROOT, exist_ok=True)

# ================= HELPERS =================
CSV_HEADER = "Archivo,Frente,Ubicacion,FechaHora\n"

def ensure_csv():
    if not os.path.exists(CSV_LOG):
        with open(CSV_LOG, "w", encoding="utf-8") as f:
            f.write(CSV_HEADER)

def frente_from_codigo(codigo: str) -> str:
    if codigo.startswith("BR"):
        return "BREMEN"
    if codigo.startswith("TALL"):
        return "TALLERES"
    if codigo.startswith("LOE"):
        return "LO ERRAZURIZ"
    return "N/A"

ensure_csv()

# ================= HANDLERS BOT =================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Envíame una *foto*.\n"
        "Luego elige el *frente/sector* (solo una pregunta):\n"
        "BR-OR, BR-PON, TALL-OR, TALL-PON, LOE-OR, LOE-PON.",
        disable_web_page_preview=True,
    )

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return

    photo_file = await update.message.photo[-1].get_file()
    fecha = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
    user = update.message.from_user
    safe_user = user.username or f"user_{user.id}"
    nombre = f"{safe_user}_{user.id}_{fecha}.jpg"

    context.user_data["pending"] = {"file": photo_file, "nombre": nombre, "fecha": fecha}

    kb = [
        [InlineKeyboardButton("BR-OR", callback_data="BR-OR"),
         InlineKeyboardButton("BR-PON", callback_data="BR-PON")],
        [InlineKeyboardButton("TALL-OR", callback_data="TALL-OR"),
         InlineKeyboardButton("TALL-PON", callback_data="TALL-PON")],
        [InlineKeyboardButton("LOE-OR", callback_data="LOE-OR"),
         InlineKeyboardButton("LOE-PON", callback_data="LOE-PON")],
    ]
    await update.message.reply_text("🏷️ Selecciona *frente/sector*:", reply_markup=InlineKeyboardMarkup(kb))
    return ASK_PRINCIPAL

async def choose_principal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    principal = q.data

    if principal not in PRINCIPAL_CHOICES:
        await q.edit_message_text("❗ Opción no válida. Intenta de nuevo.")
        return ASK_PRINCIPAL

    pending = context.user_data.get("pending")
    if not pending:
        await q.edit_message_text("⚠️ No encuentro la foto. Envíame una *foto* otra vez.")
        return ConversationHandler.END

    photo_file = pending["file"]
    nombre = pending["nombre"]
    fecha = pending["fecha"]

    subdir = os.path.join(PHOTO_SAVE_ROOT, principal)
    os.makedirs(subdir, exist_ok=True)
    dest_path = os.path.join(subdir, nombre)

    await photo_file.download_to_drive(custom_path=dest_path)

    frente = frente_from_codigo(principal)
    with open(CSV_LOG, "a", encoding="utf-8") as f:
        f.write(f"{nombre},{frente},{principal},{fecha}\n")

    context.user_data.clear()
    await q.edit_message_text(
        "✅ Guardado correctamente.\n"
        f"📁 Carpeta: {subdir}\n"
        f"🗂️ Archivo: {nombre}\n"
        f"🕒 {fecha}"
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🛑 Operación cancelada. Envía una foto para comenzar de nuevo.")
    return ConversationHandler.END

# ================= MICROSERVER (para Render) =================
async def handle_root(_):
    return web.Response(text="Bot_FotosITO OK")

async def make_web_app():
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/healthz", handle_root)
    return app

async def start_web_server():
    app = await make_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"HTTP server listening on 0.0.0.0:{port}")
    return runner

# ================= MAIN (Render friendly) =================
async def main():
    if not TOKEN:
        raise RuntimeError("🔑 BOT_TOKEN no configurado en variables de entorno.")

    # 1) Arrancar microservidor HTTP para Render
    web_runner = await start_web_server()

    # 2) Inicializar bot
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, on_photo)],
        states={ASK_PRINCIPAL: [CallbackQueryHandler(choose_principal)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(conv)

    log.info("Iniciando bot…")
    print("🤖 Bot en marcha. Esperando fotos...")

    # Arranque manual: sin run_polling (evita conflictos con el event loop de Render)
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # 3) Mantener proceso vivo hasta SIGINT/SIGTERM (Render detiene con estas señales)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows o entornos sin señales: ignorar
            pass

    await stop.wait()

    # 4) Apagado ordenado
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    await web_runner.cleanup()

# ================= ENTRYPOINT =================
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError:
        # Si el entorno ya tiene/no tiene loop, creamos uno nuevo y seguimos.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())






