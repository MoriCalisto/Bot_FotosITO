# -*- coding: utf-8 -*-
import os
import logging
import threading
import asyncio
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
BOT_TOKEN = os.getenv("BOT_TOKEN")
PHOTO_SAVE_ROOT = os.getenv("PHOTO_SAVE_ROOT", "./photos")
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID")
MS_TENANT_ID = os.getenv("MS_TENANT_ID", "common")
ONEDRIVE_ROOT = os.getenv("ONEDRIVE_ROOT", "Bot_FotosITO")
PORT = int(os.getenv("PORT", "10000"))

os.makedirs(PHOTO_SAVE_ROOT, exist_ok=True)

PRINCIPAL_CHOICES = ["BR-OR", "BR-PON", "TALL-OR", "TALL-PON", "LOE-OR", "LOE-PON"]
ASK_PRINCIPAL = 0

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")

PENDING_ONEDRIVE_FLOWS = {}

# =============== HEALTHCHECK ===============
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_health():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info(f"Healthcheck OK puerto {PORT}")

# =============== MSAL ===============
def get_graph_token():
    app = msal.PublicClientApplication(
        MS_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{MS_TENANT_ID}"
    )

    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(["Files.ReadWrite"], account=accounts[0])
        if result and "access_token" in result:
            return result["access_token"]

    raise RuntimeError("Debes ejecutar /onedrive_login")

def upload_to_onedrive(local_path, folder, filename):
    token = get_graph_token()
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{ONEDRIVE_ROOT}/{folder}/{filename}:/content"

    with open(local_path, "rb") as f:
        r = requests.put(url, headers={"Authorization": f"Bearer {token}"}, data=f)

    if r.status_code not in (200, 201):
        raise RuntimeError(r.text)

# =============== LOGIN ONEDRIVE ===============
async def onedrive_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    authority = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
    app = msal.PublicClientApplication(MS_CLIENT_ID, authority=authority)

    flow = app.initiate_device_flow(scopes=["Files.ReadWrite"])

    if "user_code" not in flow:
        await update.message.reply_text("❌ Error login OneDrive")
        return

    PENDING_ONEDRIVE_FLOWS[str(update.effective_chat.id)] = (app, flow)

    await update.message.reply_text(flow["message"])

async def onedrive_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if chat_id not in PENDING_ONEDRIVE_FLOWS:
        await update.message.reply_text("Primero ejecuta /onedrive_login")
        return

    app, flow = PENDING_ONEDRIVE_FLOWS[chat_id]

    await update.message.reply_text("⏳ Finalizando autorización...")

    try:
        result = await asyncio.to_thread(app.acquire_token_by_device_flow, flow)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return

    if "access_token" in result:
        await update.message.reply_text("✅ OneDrive listo")
        PENDING_ONEDRIVE_FLOWS.pop(chat_id, None)
    else:
        await update.message.reply_text("❌ No se pudo autorizar")

# =============== BOT ===============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 Envía una foto")

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await update.message.photo[-1].get_file()

    now = datetime.now()
    user = update.message.from_user

    usuario = user.username or user.first_name or str(user.id)
    usuario = usuario.replace(" ", "_")

    context.user_data["data"] = {
        "file": file,
        "fecha": now.strftime("%Y-%m-%d"),
        "hora": now.strftime("%H-%M-%S"),
        "usuario": usuario
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

    data = context.user_data["data"]

    nombre = f"{data['fecha']}_{data['hora']}_{q.data}_{data['usuario']}.jpg"

    path = os.path.join(PHOTO_SAVE_ROOT, q.data)
    os.makedirs(path, exist_ok=True)

    full = os.path.join(path, nombre)

    await data["file"].download_to_drive(full)

    try:
        upload_to_onedrive(full, q.data, nombre)
        msg = "☁️ Subido a OneDrive"
    except Exception as e:
        msg = f"⚠️ {e}"

    await q.edit_message_text(f"✅ {nombre}\n{msg}")

    return ConversationHandler.END

# =============== MAIN ===============
def main():
    start_health()

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, on_photo)],
        states={ASK_PRINCIPAL: [CallbackQueryHandler(choose)]},
        fallbacks=[]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("onedrive_login", onedrive_login))
    app.add_handler(CommandHandler("onedrive_finish", onedrive_finish))
    app.add_handler(conv)

    app.run_polling()

if __name__ == "__main__":
    main()
