# -*- coding: utf-8 -*-
"""
Bot de Telegram para guardar fotos en OneDrive (Render + Windows compatible)
- Pregunta solo por ubicación (BR-OR, BR-PON, TALL-OR, TALL-PON, LOE-OR, LOE-PON)
- Guarda imágenes en carpetas separadas y registra CSV
"""

import os
import asyncio
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ================= CONFIG =================
TOKEN = os.getenv("BOT_TOKEN", "").strip()
PHOTO_SAVE_ROOT = os.getenv("ONEDRIVE_ROOT", "Bot_FotosITO")
PRINCIPAL_CHOICES = ["BR-OR", "BR-PON", "TALL-OR", "TALL-PON", "LOE-OR", "LOE-PON"]
CSV_LOG = os.path.join(PHOTO_SAVE_ROOT, "registro_fotos.csv")
ASK_PRINCIPAL = 0

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("BotFotosITO")

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

# ================= HANDLERS =================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "👋 Envíame una *foto*.\n"
        "Luego elige el *frente/sector* (solo una pregunta):\n"
        "BR-OR, BR-PON, TALL-OR, TALL-PON, LOE-OR, LOE-PON."
    )
    await update.message.reply_text(txt, disable_web_page_preview=True)

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

# ================= MAIN =================
async def main():
    if not TOKEN:
        raise RuntimeError("🔑 BOT_TOKEN no configurado en variables de entorno.")
    
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, on_photo)],
        states={ASK_PRINCIPAL: [CallbackQueryHandler(choose_principal)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(conv)

    print("🤖 Bot en marcha. Esperando fotos...")

# Arranque manual compatible con Render
await app.initialize()
await app.start()
await app.updater.start_polling()

# Mantener el proceso vivo hasta que Render o tú lo detengan
await app.updater.wait_until_closed()

# Apagado ordenado
await app.stop()
await app.shutdown()

# ================= ENTRY =================
if __name__ == "__main__":
    import asyncio

    async def safe_start():
        try:
            await main()
        except RuntimeError as e:
            print(f"⚠️ Aviso: {e}")

    try:
        # Render necesita mantener el loop activo permanentemente
        asyncio.run(safe_start())
    except (RuntimeError, KeyboardInterrupt):
        # En Render esto evita el cierre del proceso
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(safe_start())
        loop.run_forever()


