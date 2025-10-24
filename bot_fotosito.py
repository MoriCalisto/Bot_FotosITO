# -*- coding: utf-8 -*-
"""
Bot de Telegram (Render) con subida directa a OneDrive (Microsoft Graph, client credentials)
- Pregunta única: BR-OR, BR-PON, TALL-OR, TALL-PON, LOE-OR, LOE-PON
- Guarda temporal en ./photos (Render) y sube a OneDrive del usuario ONEDRIVE_USER
- Incluye microservidor HTTP en $PORT para healthcheck de Render
"""

import os
import io
import asyncio
import signal
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    CallbackQueryHandler, ContextTypes, filters
)

from aiohttp import web
import requests
from msal import ConfidentialClientApplication

# ================= CONFIG =================
TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Graph / OneDrive (app-only)
CLIENT_ID = os.getenv("CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "").strip()
TENANT_ID = os.getenv("TENANT_ID", "").strip()
ONEDRIVE_USER = os.getenv("ONEDRIVE_USER", "").strip()          # ej. rmori@tuempresa.com
ONEDRIVE_ROOT = os.getenv("ONEDRIVE_ROOT", "/Bot_FotosITO").strip()

PHOTO_SAVE_ROOT = os.getenv("PHOTO_SAVE_ROOT", "./photos")
PRINCIPAL_CHOICES = ["BR-OR", "BR-PON", "TALL-OR", "TALL-PON", "LOE-OR", "LOE-PON"]
CSV_LOG = os.path.join(PHOTO_SAVE_ROOT, "registro_fotos.csv")
ASK_PRINCIPAL = 0

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("BotFotosITO")

os.makedirs(PHOTO_SAVE_ROOT, exist_ok=True)

# ================= CSV HELPERS =================
CSV_HEADER = "Archivo,Frente,Ubicacion,FechaHora\n"

def ensure_csv():
    if not os.path.exists(CSV_LOG):
        with open(CSV_LOG, "w", encoding="utf-8") as f:
            f.write(CSV_HEADER)

def frente_from_codigo(codigo: str) -> str:
    if codigo.startswith("BR"):   return "BREMEN"
    if codigo.startswith("TALL"): return "TALLERES"
    if codigo.startswith("LOE"):  return "LO ERRAZURIZ"
    return "N/A"

ensure_csv()

# ================= GRAPH (APP-ONLY) =================
def get_graph_token() -> str:
    """Obtiene un access token de Microsoft Graph usando client credentials (app-only)."""
    if not (CLIENT_ID and CLIENT_SECRET and TENANT_ID):
        raise RuntimeError("Faltan CLIENT_ID / CLIENT_SECRET / TENANT_ID en variables de entorno.")

    app = ConfidentialClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        client_credential=CLIENT_SECRET,
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise RuntimeError(f"Error autenticando con Graph: {result.get('error_description')}")
    return result["access_token"]

def upload_to_onedrive(local_path: str, remote_folder: str, filename: str):
    """
    Sube el archivo local a OneDrive del usuario especificado (ONEDRIVE_USER)
    Ruta destino: /<ONEDRIVE_ROOT>/<remote_folder>/<filename>
    """
    token = get_graph_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
    }

    # Normalizamos el path destino dentro del drive
    remote_path = f"{ONEDRIVE_ROOT}/{remote_folder}/{filename}".replace("//", "/")
    # Endpoint: /users/{user}/drive/root:/path:/content
    url = f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER}/drive/root:{remote_path}:/content"

    with open(local_path, "rb") as f:
        r = requests.put(url, headers=headers, data=f, timeout=60)
    if r.status_code >= 200 and r.status_code < 300:
        log.info(f"📤 Subida a OneDrive OK: {remote_path}")
    else:
        log.error(f"❌ Error subiendo a OneDrive: {r.status_code} - {r.text}")
        # no interrumpimos el bot; queda registrado

# ================= HANDLERS BOT =================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Envíame una *foto*.\n"
        "Luego elige el *frente/sector* (una sola pregunta):\n"
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

    # Guardado temporal en Render (por si falla la red, queda registro)
    subdir = os.path.join(PHOTO_SAVE_ROOT, principal)
    os.makedirs(subdir, exist_ok=True)
    dest_path = os.path.join(subdir, nombre)
    await photo_file.download_to_drive(custom_path=dest_path)

    # Subir a OneDrive
    try:
        upload_to_onedrive(dest_path, principal, nombre)
    except Exception as e:
        log.exception(f"Error en subida OneDrive: {e}")

    # CSV
    frente = frente_from_codigo(principal)
    with open(CSV_LOG, "a", encoding="utf-8") as f:
        f.write(f"{nombre},{frente},{principal},{fecha}\n")

    context.user_data.clear()
    await q.edit_message_text(
        "✅ Guardado correctamente.\n"
        f"📁 Carpeta: {ONEDRIVE_ROOT}/{principal}\n"
        f"🗂️ Archivo: {nombre}\n"
        f"🕒 {fecha}"
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🛑 Operación cancelada. Envía una foto para comenzar de nuevo.")
    return ConversationHandler.END

# ================= MICROSERVER (healthcheck) =================
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
        raise RuntimeError("🔑 BOT_TOKEN no configurado (Environment).")

    # HTTP para healthcheck de Render
    web_runner = await start_web_server()

    # Bot
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

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # Mantener vivo hasta señal de Render
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    await stop.wait()

    # Shutdown ordenado
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    await web_runner.cleanup()

# ================= ENTRYPOINT =================
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())






