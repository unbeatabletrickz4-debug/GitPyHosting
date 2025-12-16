import os
import logging
import asyncio
import subprocess
import signal
import psutil
import json
import threading
import shutil
from urllib.parse import quote_plus

from flask import Flask, request
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, filters, ConversationHandler, CallbackQueryHandler
)

# =========================
# CONFIG
# =========================
TOKEN = os.environ.get("TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")

UPLOAD_DIR = "scripts"
os.makedirs(UPLOAD_DIR, exist_ok=True)

USERS_FILE = "allowed_users.json"
OWNERSHIP_FILE = "ownership.json"

running_processes = {}  # {target_id: {"process": Popen, "log": log_path}}

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# =========================
# FLASK SERVER
# =========================
app = Flask(__name__)

@app.route('/')
def home():
    return "🤖 Python Host Bot is Alive!", 200

@app.route('/status')
def script_status():
    script_name = request.args.get('script')
    if not script_name:
        return "Specify script", 400

    if script_name in running_processes and running_processes[script_name]['process'].poll() is None:
        return f"✅ {script_name} is running.", 200
    return f"❌ {script_name} is stopped.", 404

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# =========================
# DATA MANAGEMENT
# =========================
def get_allowed_users():
    if not os.path.exists(USERS_FILE):
        return []
    try:
        with open(USERS_FILE, 'r') as f:
            return json.load(f)
    except:
        return []

def save_allowed_user(uid: int):
    users = get_allowed_users()
    if uid not in users:
        users.append(uid)
        with open(USERS_FILE, 'w') as f:
            json.dump(users, f)
        return True
    return False

def remove_allowed_user(uid: int):
    users = get_allowed_users()
    if uid in users:
        users.remove(uid)
        with open(USERS_FILE, 'w') as f:
            json.dump(users, f)
        return True
    return False

def load_ownership():
    if not os.path.exists(OWNERSHIP_FILE):
        return {}
    try:
        with open(OWNERSHIP_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_ownership(target_id: str, user_id: int, type_: str):
    data = load_ownership()
    data[target_id] = {"owner": user_id, "type": type_}
    with open(OWNERSHIP_FILE, 'w') as f:
        json.dump(data, f)

def delete_ownership(target_id: str):
    data = load_ownership()
    if target_id in data:
        del data[target_id]
        with open(OWNERSHIP_FILE, 'w') as f:
            json.dump(data, f)

def get_owner(target_id: str):
    data = load_ownership()
    return data.get(target_id, {}).get("owner")

# =========================
# DECORATORS
# =========================
def restricted(func):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        uid = update.effective_user.id
        if uid != ADMIN_ID and uid not in get_allowed_users():
            if update.message:
                await update.message.reply_text("⛔ Access Denied.")
            else:
                await update.callback_query.message.reply_text("⛔ Access Denied.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

def super_admin_only(func):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("⛔ Super Admin Only.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# =========================
# KEYBOARDS
# =========================
def main_menu_keyboard():
    return ReplyKeyboardMarkup([
        ["📤 Upload File", "🌐 Clone from Git"],
        ["🚀 Deploy to Render", "📂 My Hosted Apps"],
        ["📊 Server Stats", "🆘 Help"]
    ], resize_keyboard=True)

def extras_keyboard():
    return ReplyKeyboardMarkup(
        [["➕ Add reqs", "➕ Add .env"], ["🚀 RUN NOW", "🔙 Cancel"]],
        resize_keyboard=True
    )

# =========================
# REQUIREMENTS INSTALL
# =========================
def smart_fix_requirements(req_path):
    try:
        with open(req_path, 'r') as f:
            lines = f.readlines()
        clean = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.lower().startswith("pip install"):
                clean.extend(line[11:].strip().split())
            else:
                clean.append(line)
        with open(req_path, 'w') as f:
            f.write('\n'.join(clean))
        return True
    except:
        return False

async def install_requirements(req_path, update):
    msg = await update.message.reply_text("⏳ Installing requirements...")
    smart_fix_requirements(req_path)
    try:
        proc = await asyncio.create_subprocess_exec(
            "pip", "install", "-r", req_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0:
            await msg.edit_text("✅ Installed!")
        else:
            await msg.edit_text(f"❌ Failed:\n{stderr.decode()[-900:]}")
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")

# =========================
# BASIC COMMANDS
# =========================
@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Python & Git Hosting Bot", reply_markup=main_menu_keyboard())

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("🚫 Operation Cancelled.", reply_markup=main_menu_keyboard())
    else:
        await update.callback_query.message.reply_text("🚫 Operation Cancelled.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# =========================
# CONVERSATION 1: UPLOAD FILE
# =========================
WAIT_PY, WAIT_EXTRAS = range(2)

@restricted
async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📤 Send a `.py` file.", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
    return WAIT_PY

async def receive_py(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await update.message.document.get_file()
    fname = update.message.document.file_name
    uid = update.effective_user.id

    if not fname.endswith(".py"):
        await update.message.reply_text("❌ Only .py allowed")
        return WAIT_PY

    owner = get_owner(fname)
    if os.path.exists(os.path.join(UPLOAD_DIR, fname)) and owner and owner != uid and uid != ADMIN_ID:
        await update.message.reply_text(f"❌ Taken! `{fname}` is owned by another user.")
        return WAIT_PY

    path = os.path.join(UPLOAD_DIR, fname)
    await file.download_to_drive(path)
    save_ownership(fname, uid, "file")

    context.user_data['type'] = 'file'
    context.user_data['target_id'] = fname
    context.user_data['work_dir'] = UPLOAD_DIR
    context.user_data['wait'] = None

    await update.message.reply_text("✅ Saved. Add extras (optional)?", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

async def receive_extras(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text

    if txt == "🚀 RUN NOW":
        return await execute_logic(update, context)

    if "reqs" in txt:
        await update.message.reply_text("📂 Send `requirements.txt` now.")
        context.user_data['wait'] = 'req'
    elif ".env" in txt:
        await update.message.reply_text("🔒 Send `.env` now.")
        context.user_data['wait'] = 'env'

    return WAIT_EXTRAS

async def receive_extra_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wait = context.user_data.get('wait')
    if not wait:
        return WAIT_EXTRAS

    file = await update.message.document.get_file()
    fname = update.message.document.file_name

    target_id = context.user_data['target_id']
    work_dir = context.user_data['work_dir']
    type_ = context.user_data['type']

    prefix = target_id if type_ == 'file' else target_id.split("|")[0]

    if wait == 'req' and fname.endswith('.txt'):
        path = os.path.join(work_dir, f"{prefix}_req.txt")
        await file.download_to_drive(path)
        await install_requirements(path, update)

    elif wait == 'env' and fname.endswith('.env'):
        if type_ == 'file':
            # scripts/<script.py>.env
            path = os.path.join(work_dir, f"{target_id}.env")
        else:
            # scripts/<repo>/.env
            path = os.path.join(work_dir, ".env")
        await file.download_to_drive(path)
        await update.message.reply_text("✅ Env saved.")

    context.user_data['wait'] = None
    await update.message.reply_text("Next?", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

# =========================
# CONVERSATION 2: GIT CLONE
# =========================
WAIT_URL, WAIT_SELECT_FILE, WAIT_GIT_EXTRAS = range(2, 5)

@restricted
async def git_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🌐 Send PUBLIC Git repo URL", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
    return WAIT_URL

async def receive_git_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not url.startswith("http"):
        await update.message.reply_text("❌ Invalid URL.")
        return WAIT_URL

    repo_name = url.split("/")[-1].replace(".git", "")
    repo_path = os.path.join(UPLOAD_DIR, repo_name)

    msg = await update.message.reply_text(f"⏳ Cloning `{repo_name}` ...")

    if os.path.exists(repo_path):
        shutil.rmtree(repo_path)

    try:
        subprocess.check_call(["git", "clone", url, repo_path])
        await msg.edit_text("✅ Cloned Successfully!")

        py_files = []
        for root, dirs, files in os.walk(repo_path):
            for file in files:
                if file.endswith(".py"):
                    rel_path = os.path.relpath(os.path.join(root, file), repo_path)
                    py_files.append(rel_path)

        if not py_files:
            await update.message.reply_text("❌ No .py found.", reply_markup=main_menu_keyboard())
            return ConversationHandler.END

        context.user_data['repo_path'] = repo_path
        context.user_data['repo_name'] = repo_name

        req_path = os.path.join(repo_path, "requirements.txt")
        if os.path.exists(req_path):
            await update.message.reply_text("📦 Installing repo requirements.txt ...")
            await install_requirements(req_path, update)

        keyboard = []
        for f in py_files[:12]:
            keyboard.append([InlineKeyboardButton(f, callback_data=f"sel_py_{f}")])

        await update.message.reply_text("👇 Select main file:", reply_markup=InlineKeyboardMarkup(keyboard))
        return WAIT_SELECT_FILE

    except Exception as e:
        await msg.edit_text(f"❌ Clone Failed: {e}")
        return ConversationHandler.END

async def select_git_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    filename = query.data.split("sel_py_")[1]
    repo_path = context.user_data['repo_path']
    repo_name = context.user_data['repo_name']
    uid = update.effective_user.id

    unique_id = f"{repo_name}|{filename}"
    save_ownership(unique_id, uid, "repo")

    context.user_data['type'] = 'repo'
    context.user_data['target_id'] = unique_id
    context.user_data['work_dir'] = repo_path
    context.user_data['wait'] = None

    await query.edit_message_text(f"✅ Selected `{filename}`")
    await query.message.reply_text("Add extras (optional) then RUN:", reply_markup=extras_keyboard())
    return WAIT_GIT_EXTRAS

async def git_extras_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await receive_extras(update, context)

async def git_extra_files_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await receive_extra_files(update, context)

# =========================
# CONVERSATION 3: DEPLOY LINK
# =========================
WAIT_DEPLOY_REPO = 10

@restricted
async def deploy_render_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 Deploy to Render\n\nSend your PUBLIC GitHub repo URL.\nExample:\nhttps://github.com/user/repo",
        reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True)
    )
    return WAIT_DEPLOY_REPO

async def deploy_render_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if not raw.startswith("http"):
        await update.message.reply_text("❌ Invalid URL.")
        return WAIT_DEPLOY_REPO

    repo = raw.replace(".git", "").strip()
    deploy_url = f"https://render.com/deploy?repo={quote_plus(repo)}"

    await update.message.reply_text(
        "✅ Render Deploy Link:\n"
        f"`{deploy_url}`\n\n"
        "Open it in browser and deploy.\n"
        "Tip: add render.yaml in repo for 1-click blueprint deploy.",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# =========================
# EXECUTION ENGINE
# =========================
async def execute_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_func = update.message.reply_text if update.message else update.callback_query.message.reply_text

    target_id = context.user_data.get('target_id', context.user_data.get('fallback_id'))
    if not target_id:
        await msg_func("❌ Missing target ID", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    # Decide work_dir, script_path, env_path
    if "|" in target_id:
        repo, file_rel = target_id.split("|", 1)
        work_dir = os.path.join(UPLOAD_DIR, repo)
        script_path = file_rel
        env_path = os.path.join(work_dir, ".env")  # repo env stored here
    else:
        work_dir = UPLOAD_DIR
        script_path = target_id
        env_path = os.path.join(work_dir, f"{target_id}.env")  # file env stored here

    # Already running?
    if target_id in running_processes and running_processes[target_id]['process'].poll() is None:
        await msg_func(f"⚠️ `{target_id}` is already running!", reply_markup=main_menu_keyboard(), parse_mode="Markdown")
        return ConversationHandler.END

    # Load env overrides
    custom_env = os.environ.copy()
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                custom_env[k.strip()] = v.strip()

    log_file_path = os.path.join(UPLOAD_DIR, f"{target_id.replace('|','_')}.log")
    log_file = open(log_file_path, "w", buffering=1)

    try:
        proc = subprocess.Popen(
            ["python", "-u", script_path],
            env=custom_env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=work_dir,
            preexec_fn=os.setsid
        )
        running_processes[target_id] = {"process": proc, "log": log_file_path}

        await msg_func(f"🚀 Started!\nID: `{target_id}`\nPID: {proc.pid}", parse_mode="Markdown")

        await asyncio.sleep(2)
        if proc.poll() is not None:
            log_file.close()
            with open(log_file_path, "r") as f:
                tail = f.read()[-2000:]
            await msg_func(f"❌ Crashed:\n```\n{tail}\n```", parse_mode="Markdown", reply_markup=main_menu_keyboard())
        else:
            url = f"{BASE_URL}/status?script={target_id}"
            await msg_func(f"🟢 Running!\n🔗 `{url}`", parse_mode="Markdown", reply_markup=main_menu_keyboard())

    except Exception as e:
        await msg_func(f"❌ Error: {e}", reply_markup=main_menu_keyboard())

    return ConversationHandler.END

# =========================
# LIST + MANAGE
# =========================
@restricted
async def list_hosted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ownership = load_ownership()
    keyboard = []

    for tid, meta in ownership.items():
        owner = meta.get("owner")
        if uid == ADMIN_ID or uid == owner:
            is_running = tid in running_processes and running_processes[tid]['process'].poll() is None
            status = "🟢" if is_running else "🔴"
            label = f"{status} {tid}"
            if uid == ADMIN_ID and uid != owner:
                label += f" (User: {owner})"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"man_{tid}")])

    if not keyboard:
        await update.message.reply_text("📂 No hosted apps.", reply_markup=main_menu_keyboard())
        return

    await update.message.reply_text("📂 Your Apps:", reply_markup=InlineKeyboardMarkup(keyboard))

async def manage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = update.effective_user.id

    if data.startswith("sel_py_"):
        return await select_git_file(update, context)

    if data.startswith("man_"):
        target_id = data.split("man_")[1]
        owner = get_owner(target_id)
        if uid != ADMIN_ID and uid != owner:
            return await query.message.reply_text("⛔ Not yours.")

        is_running = target_id in running_processes and running_processes[target_id]['process'].poll() is None
        text = f"⚙️ Manage: `{target_id}`\nStatus: {'🟢 Running' if is_running else '🔴 Stopped'}"
        btns = []
        if is_running:
            btns.append([InlineKeyboardButton("🛑 Stop", callback_data=f"stop_{target_id}")])
            btns.append([InlineKeyboardButton("🔗 URL", callback_data=f"url_{target_id}")])
        else:
            btns.append([InlineKeyboardButton("🚀 Run", callback_data=f"rerun_{target_id}")])

        btns.append([InlineKeyboardButton("📜 Logs", callback_data=f"log_{target_id}")])
        btns.append([InlineKeyboardButton("🗑️ Delete", callback_data=f"del_{target_id}")])

        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")
        return

    if data.startswith("stop_"):
        tid = data.split("stop_")[1]
        if tid in running_processes and running_processes[tid]['process'].poll() is None:
            try:
                os.killpg(os.getpgid(running_processes[tid]['process'].pid), signal.SIGTERM)
            except:
                pass
            running_processes[tid]['process'].wait()
            await query.edit_message_text(f"🛑 Stopped `{tid}`", parse_mode="Markdown")
        else:
            await query.message.reply_text("⚠️ Already stopped.")
        return

    if data.startswith("rerun_"):
        context.user_data['fallback_id'] = data.split("rerun_")[1]
        await query.delete_message()
        return await execute_logic(update, context)

    if data.startswith("url_"):
        tid = data.split("url_")[1]
        await query.message.reply_text(f"🔗 `{BASE_URL}/status?script={tid}`", parse_mode="Markdown")
        return

    if data.startswith("log_"):
        tid = data.split("log_")[1]
        path = os.path.join(UPLOAD_DIR, f"{tid.replace('|','_')}.log")
        if os.path.exists(path):
            await cont
