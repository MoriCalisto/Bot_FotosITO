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
    raise RuntimeError("Define BOT_TOKEN en Render.")

PHOTO_SAVE_ROOT = os.getenv("PHOTO_SAVE_ROOT", "./photos")
os.makedirs(PHOTO_SAVE_ROOT, exist_ok=True)

PRINCIPAL_CHOICES = ["BR-OR", "BR-PON", "TALL-OR", "TALL-PON", "LOE-OR", "LOE-PON"]
CSV_LOG = os.path.join(PHOTO_SAVE_ROOT, "registro_fotos.csv")
CSV_HEADER = "Archivo,Frente,Ubicacion,FechaHora\n"
ASK_PRINCIPAL = 0

MS_CLIENT_ID = os.getenv("MS_CLIENT_ID", "")
MS_TENANT_ID = os.getenv("MS_TENANT_ID", "common")
MS_SCOPES = ["Files.ReadWrite"]
ONEDRIVE_ROOT = os.getenv("ONEDRIVE_ROOT", "Bot_FotosITO")
TOKEN_CACHE_PATH = os.getenv("TOKEN_CACHE_PATH", "./token_cache.bin")

PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BotFotosITO")

PENDING_ONEDRIVE_FLOWS = {}

# =============== HEALTHCHECK ===============
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_healthcheck():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info(f"Healthcheck activo en puerto {PORT}")

# =============== CSV ===============
def ensure_csv():
    if not os.path.exists(CSV_LOG):
        with open(CSV_LOG, "w") as f:
            f.write(CSV_HEADER)

ensure_csv()

# =============== MSAL ===============
def load_cache():
    cache = msal.SerializableTokenCache()
    if os.path.exists(TOKEN_CACHE_PATH):
        cache.deserialize(open(TOKEN_CACHE_PATH).read())
    return cache

def save_cache(cache):
    if cache.has_state_changed:
        open(TOKEN_CACHE_PATH, "w").write(cache.serialize())

def get_graph_token():
    cache = load_cache()
    app = msal.PublicClientApplication(
        MS_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{MS_TENANT_ID}",
        token_cache=cache,
    )

    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(MS_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            return result["access_token"]

    raise RuntimeError("Debes ejecutar /onedrive_login")

def upload_to_onedrive(local_path, remote_dir, filename):
    token = get_graph_token()
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{ONEDRIVE_ROOT}/{remote_dir}/{filename}:/content"

    with open(local_path, "rb") as f:
        r = requests.put(url, headers={"Authorization": f"Bearer {token}"}, data=f)

    if r.status_code not in (200, 201):
        raise RuntimeError(r.text)

# =============== COMMANDS ===============
async def onedrive_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app = msal.PublicClientApplication(MS_CLIENT_ID)
    flow = app.initiate_device_flow(scopes=MS_SCOPES)

    PENDING_ONEDRIVE_FLOWS[str(update.effective_chat.id)] = (app, flow)

    await update.message.reply_text(flow["message"])

async def onedrive_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if chat_id not in PENDING_ONEDRIVE_FLOWS:
        await update.message.reply_text("Primero ejecuta /onedrive_login")
        return

    app, flow = PENDING_ONEDRIVE_FLOWS[chat_id]
    result = app.acquire_token_by_device_flow(flow)

    if "access_token" in result:
        save_cache(app.token_cache)
        await update.message.reply_text("✅ OneDrive listo")
    else:
        await update.message.reply_text("❌ Error en autorización")

# =============== HANDLERS ===============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Envía una foto")

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = await update.message.photo[-1].get_file()

    now = datetime.now()
    user = update.message.from_user
    usuario = user.username or user.first_name or str(user.id)
    usuario = usuario.replace(" ", "_")

    context.user_data["pending"] = {
        "file": photo,
        "fecha": now.strftime("%Y-%m-%d"),
        "hora": now.strftime("%H-%M-%S"),
        "usuario": usuario,
    }

    kb = [[InlineKeyboardButton(x, callback_data=x)] for x in PRINCIPAL_CHOICES]

    await update.message.reply_text(
        "Selecciona frente",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return ASK_PRINCIPAL

async def choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = context.user_data["pending"]

    nombre = f"{data['fecha']}_{data['hora']}_{q.data}_{data['usuario']}.jpg"

    path = os.path.join(PHOTO_SAVE_ROOT, q.data)
    os.makedirs(path, exist_ok=True)

    full_path = os.path.join(path, nombre)

    await data["file"].download_to_drive(full_path)

    with open(CSV_LOG, "a") as f:
        f.write(f"{nombre},{q.data},{q.data},{data['fecha']}\n")

    try:
        upload_to_onedrive(full_path, q.data, nombre)
        msg = "☁️ Subido a OneDrive"
    except Exception as e:
        msg = f"⚠️ {e}"

    await q.edit_message_text(f"✅ Guardado\n{nombre}\n{msg}")

    return ConversationHandler.END

# =============== MAIN ===============
def main():
    start_healthcheck()

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, on_photo)],
        states={ASK_PRINCIPAL: [CallbackQueryHandler(choose)]},
        fallbacks=[],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("onedrive_login", onedrive_login))
    app.add_handler(CommandHandler("onedrive_finish", onedrive_finish))
    app.add_handler(conv)

    app.run_polling()

if __name__ == "__main__":
    main()
