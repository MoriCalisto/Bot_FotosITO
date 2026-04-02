# -*- coding: utf-8 -*-
import os
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =============== CONFIG ===============
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("Define BOT_TOKEN en Render (Environment > Secret).")

PHOTO_SAVE_ROOT = os.getenv("PHOTO_SAVE_ROOT", "./photos")
os.makedirs(PHOTO_SAVE_ROOT, exist_ok=True)

PRINCIPAL_CHOICES = ["BR-OR", "BR-PON", "TALL-OR", "TALL-PON", "LOE-OR", "LOE-PON"]
CSV_LOG = os.path.join(PHOTO_SAVE_ROOT, "registro_fotos.csv")
CSV_HEADER = "Archivo,Frente,Ubicacion,FechaHora\n"
ASK_PRINCIPAL = 0

# Healthcheck port para Render Web Service
PORT = int(os.getenv("PORT", "10000"))

# =============== LOGGING ===============
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("BotFotosITO")


# =============== HEALTHCHECK SERVER ===============
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


def start_healthcheck_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f"Healthcheck HTTP server escuchando en 0.0.0.0:{PORT}")
    return server


# =============== CSV ===============
def ensure_csv():
    if not os.path.exists(CSV_LOG):
        with open(CSV_LOG, "w", encoding="utf-8") as f:
            f.write(CSV_HEADER)
        return

    try:
        with open(CSV_LOG, "r", encoding="utf-8") as f:
            first = f.readline()

        if first.strip() != CSV_HEADER.strip():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = os.path.join(PHOTO_SAVE_ROOT, f"registro_fotos-{ts}.bak.csv")
            os.replace(CSV_LOG, backup)

            with open(CSV_LOG, "w", encoding="utf-8") as f:
                f.write(CSV_HEADER)

            log.info(f"CSV antiguo respaldado como: {backup}")
    except Exception as e:
        log.warning(f"No se pudo validar CSV, recreando: {e}")
        with open(CSV_LOG, "w", encoding="utf-8") as f:
            f.write(CSV_HEADER)


ensure_csv()


# =============== UTILS ===============
def frente_from_codigo(codigo: str) -> str:
    if codigo.startswith("BR"):
        return "BREMEN"
    if codigo.startswith("TALL"):
        return "TALLERES"
    if codigo.startswith("LOE"):
        return "LO ERRAZURIZ"
    return "N/A"


def ensure_saved(path: str) -> None:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if os.path.getsize(path) <= 0:
        raise IOError("Archivo vacío")


# =============== HANDLERS ===============
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Envíame una foto.\n"
        "Luego elige el frente/sector:\n"
        "BR-OR, BR-PON, TALL-OR, TALL-PON, LOE-OR, LOE-PON.\n\n"
        f"📂 Local: {os.path.abspath(PHOTO_SAVE_ROOT)}\n"
        "☁️ OneDrive: desactivado temporalmente en V1"
    )


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return

    photo_file = await update.message.photo[-1].get_file()
    fecha_hora = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
    user = update.message.from_user
    safe_user = user.username or "user"
    nombre_archivo = f"{safe_user}_{user.id}_{fecha_hora}.jpg"

    context.user_data["pending"] = {
        "file": photo_file,
        "nombre": nombre_archivo,
        "fecha": fecha_hora,
    }

    kb = [
        [
            InlineKeyboardButton("BR-OR", callback_data="BR-OR"),
            InlineKeyboardButton("BR-PON", callback_data="BR-PON"),
        ],
        [
            InlineKeyboardButton("TALL-OR", callback_data="TALL-OR"),
            InlineKeyboardButton("TALL-PON", callback_data="TALL-PON"),
        ],
        [
            InlineKeyboardButton("LOE-OR", callback_data="LOE-OR"),
            InlineKeyboardButton("LOE-PON", callback_data="LOE-PON"),
        ],
    ]

    await update.message.reply_text(
        "🏷️ Selecciona frente/sector:",
        reply_markup=InlineKeyboardMarkup(kb),
    )
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
        await q.edit_message_text("⚠️ No encuentro la foto. Envía una foto otra vez.")
        return ConversationHandler.END

    photo_file = pending["file"]
    nombre = pending["nombre"]
    fecha = pending["fecha"]

    subdir = os.path.join(PHOTO_SAVE_ROOT, principal)
    os.makedirs(subdir, exist_ok=True)
    dest_path = os.path.join(subdir, nombre)

    await photo_file.download_to_drive(custom_path=dest_path)
    ensure_saved(dest_path)

    frente = frente_from_codigo(principal)
    with open(CSV_LOG, "a", encoding="utf-8") as f:
        f.write(f"{nombre},{frente},{principal},{fecha}\n")

    # OneDrive desactivado temporalmente para dejar V1 estable
    od_note = "ℹ️ OneDrive desactivado temporalmente en V1."

    context.user_data.clear()
    await q.edit_message_text(
        "✅ Guardado.\n"
        f"📁 Local: {os.path.abspath(subdir)}\n"
        f"🗂️ Archivo: {nombre}\n"
        f"🕒 {fecha}\n"
        f"{od_note}"
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🛑 Cancelado. Envía una foto para comenzar de nuevo.")
    return ConversationHandler.END


def main():
    start_healthcheck_server()

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, on_photo)],
        states={ASK_PRINCIPAL: [CallbackQueryHandler(choose_principal)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(conv)

    log.info(
        f"Bot iniciado. Guardando local en: {os.path.abspath(PHOTO_SAVE_ROOT)}"
    )

    app.run_polling()


if __name__ == "__main__":
    main()
