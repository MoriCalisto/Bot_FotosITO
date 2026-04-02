# -*- coding: utf-8 -*-
import os
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import msal

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

# OneDrive / Graph
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID", "")
MS_TENANT_ID = os.getenv("MS_TENANT_ID", "common")
MS_SCOPES = ["Files.ReadWrite"]
ONEDRIVE_ROOT = os.getenv("ONEDRIVE_ROOT", "Bot_FotosITO")
TOKEN_CACHE_PATH = os.getenv("TOKEN_CACHE_PATH", "./token_cache.bin")

# Render healthcheck
PORT = int(os.getenv("PORT", "10000"))

# =============== LOGGING ===============
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("BotFotosITO")

# Guardamos flows temporales por chat para terminar login manualmente
PENDING_ONEDRIVE_FLOWS = {}


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


# ---------- MSAL helpers ----------
def load_cache():
    cache = msal.SerializableTokenCache()
    if os.path.exists(TOKEN_CACHE_PATH):
        try:
            with open(TOKEN_CACHE_PATH, "r", encoding="utf-8") as f:
                cache.deserialize(f.read())
        except Exception:
            pass
    return cache


def save_cache(cache):
    if cache.has_state_changed:
        with open(TOKEN_CACHE_PATH, "w", encoding="utf-8") as f:
            f.write(cache.serialize())


def get_graph_token():
    if not MS_CLIENT_ID:
        raise RuntimeError("Define MS_CLIENT_ID en Render (tu App Client ID).")

    authority = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
    cache = load_cache()
    app = msal.PublicClientApplication(
        MS_CLIENT_ID,
        authority=authority,
        token_cache=cache,
    )

    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(MS_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            save_cache(cache)
            return result["access_token"]

    raise RuntimeError(
        "OneDrive no autorizado aún. Usa /onedrive_login y luego /onedrive_finish."
    )


def upload_to_onedrive(local_path: str, remote_dir: str, filename: str):
    token = get_graph_token()
    remote_path = f"/{ONEDRIVE_ROOT}/{remote_dir}/{filename}".replace("//", "/")
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:{remote_path}:/content"

    with open(local_path, "rb") as f:
        r = requests.put(url, headers={"Authorization": f"Bearer {token}"}, data=f)

    if r.status_code not in (200, 201):
        raise RuntimeError(f"Graph upload error {r.status_code}: {r.text}")


# =============== COMMANDS ===============
async def cmd_onedrive_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if not MS_CLIENT_ID:
        await update.message.reply_text("❌ Falta MS_CLIENT_ID en Render.")
        return

    authority = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
    cache = load_cache()
    app = msal.PublicClientApplication(
        MS_CLIENT_ID,
        authority=authority,
        token_cache=cache,
    )

    flow = app.initiate_device_flow(scopes=MS_SCOPES)
    if "user_code" not in flow:
        await update.message.reply_text("❌ No se pudo iniciar el login de OneDrive.")
        return

    chat_id = str(update.message.chat_id)
    PENDING_ONEDRIVE_FLOWS[chat_id] = {
        "flow": flow,
        "cache": cache,
        "created_at": datetime.now().isoformat(),
    }

    mensaje = flow.get("message", "")
    log.info(f"Autoriza OneDrive manual: {mensaje}")

    await update.message.reply_text(
        "🔐 Autorización OneDrive iniciada.\n\n"
        f"{mensaje}\n\n"
        "Cuando termines en Microsoft, vuelve aquí y escribe:\n"
        "/onedrive_finish"
    )


async def cmd_onedrive_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = str(update.message.chat_id)
    pending = PENDING_ONEDRIVE_FLOWS.get(chat_id)

    if not pending:
        await update.message.reply_text(
            "⚠️ No hay una autorización pendiente.\n"
            "Primero ejecuta /onedrive_login"
        )
        return

    flow = pending["flow"]
    cache = pending["cache"]

    authority = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
    app = msal.PublicClientApplication(
        MS_CLIENT_ID,
        authority=authority,
        token_cache=cache,
    )

    result = app.acquire_token_by_device_flow(flow)

    if "access_token" in result:
        save_cache(cache)
        PENDING_ONEDRIVE_FLOWS.pop(chat_id, None)
        await update.message.reply_text("✅ OneDrive autorizado correctamente.")
    else:
        await update.message.reply_text(
            "❌ Aún no se pudo obtener el token.\n"
            f"Detalle: {result.get('error_description', 'sin detalle')}"
        )


# =============== HANDLERS ===============
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Envíame una foto.\n"
        "Luego elige el frente/sector:\n"
        "BR-OR, BR-PON, TALL-OR, TALL-PON, LOE-OR, LOE-PON.\n\n"
        "Comandos útiles:\n"
        "/onedrive_login → iniciar autorización OneDrive\n"
        "/onedrive_finish → terminar autorización OneDrive\n\n"
        f"📂 Local: {os.path.abspath(PHOTO_SAVE_ROOT)}\n"
        f"☁️ OneDrive: /{ONEDRIVE_ROOT}/<frente>/archivo.jpg"
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

    try:
        upload_to_onedrive(dest_path, remote_dir=principal, filename=nombre)
        od_note = "☁️ Subida a OneDrive OK."
    except Exception as e:
        od_note = f"⚠️ OneDrive falló: {e}"
        log.error(od_note)

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


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled exception", exc_info=context.error)


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
    app.add_handler(CommandHandler("onedrive_login", cmd_onedrive_login))
    app.add_handler(CommandHandler("onedrive_finish", cmd_onedrive_finish))
    app.add_handler(conv)
    app.add_error_handler(error_handler)

    log.info(
        f"Bot iniciado. Guardando local en: {os.path.abspath(PHOTO_SAVE_ROOT)}  | OneDrive root: /{ONEDRIVE_ROOT}"
    )

    app.run_polling()


if __name__ == "__main__":
    main()
