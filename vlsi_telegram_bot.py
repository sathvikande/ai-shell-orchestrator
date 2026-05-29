import logging
import asyncio
import os
import subprocess
import psycopg2
import urllib.request
import urllib.parse
import re
import json
import concurrent.futures
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import xml.etree.ElementTree as ET
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, 
    CommandHandler, 
    MessageHandler, 
    CallbackQueryHandler,
    filters, 
    ContextTypes
)
from telegram.request import HTTPXRequest
from litellm import acompletion

# ==========================================
# 1. Logging & Cloud PostgreSQL Database
# ==========================================
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS command_audit (id SERIAL PRIMARY KEY, timestamp TEXT, user_id BIGINT, command TEXT, status TEXT, output TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS chat_memory (id SERIAL PRIMARY KEY, user_id BIGINT, role TEXT, content TEXT, timestamp TEXT)''')
    conn.commit()
    cursor.close()
    conn.close()

def log_audit(user_id, command, status, output):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''INSERT INTO command_audit (timestamp, user_id, command, status, output) VALUES (%s, %s, %s, %s, %s)''', (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id, command, status, output))
    conn.commit()
    cursor.close()
    conn.close()

def save_chat_memory(user_id, role, content):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO chat_memory (user_id, role, content, timestamp) VALUES (%s, %s, %s, %s)", (user_id, role, content, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    cursor.close()
    conn.close()

def get_chat_history(user_id, limit=8):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT role, content FROM chat_memory WHERE user_id = %s ORDER BY id DESC LIMIT %s", (user_id, limit))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

# ==========================================
# 2. HYBRID WEB SYNTHESIS ENGINE
# ==========================================
def clean_html(raw_html):
    text = re.sub(r'<script.*?</script>', '', raw_html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<.*?>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()

def fetch_single_site(url):
    try:
        jina_req = urllib.request.Request(f"https://r.jina.ai/{url}", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(jina_req, timeout=8) as response:
            return response.read().decode('utf-8')[:2500] 
    except:
        try:
            raw_req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(raw_req, timeout=5) as response:
                return clean_html(response.read().decode('utf-8', errors='ignore'))[:2500]
        except:
            return "[Failed to read this source]"

def web_browse(query):
    try:
        target_urls = [p for p in query.split() if p.startswith("http://") or p.startswith("https://")]
        news_summary = ""
        
        if not target_urls:
            try:
                safe_q = urllib.parse.quote(f"{query} when:1d")
                req = urllib.request.Request(f"https://news.google.com/rss/search?q={safe_q}&hl=en-US&gl=US&ceid=US:en", headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=5) as response:
                    items = ET.fromstring(response.read()).findall('.//item')[:4]
                    if items: news_summary = "📰 **LATEST HEADLINES:**\n" + "\n".join([f"- {i.find('title').text}" for i in items]) + "\n\n"
            except: pass

            try:
                req = urllib.request.Request("https://lite.duckduckgo.com/lite/", data=urllib.parse.urlencode({'q': query}).encode('utf-8'), headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                with urllib.request.urlopen(req, timeout=8) as response:
                    target_urls = [urllib.parse.unquote(u) for u in re.findall(r'href="\/\/duckduckgo\.com\/l\/\?uddg=([^"&]+)', response.read().decode('utf-8'))][:3]
            except: pass

        results = [news_summary] if news_summary else []
        if target_urls:
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                for url, content in zip(target_urls, list(executor.map(fetch_single_site, target_urls))):
                    if content and "[Failed" not in content: results.append(f"🌐 **Source:** {url}\n{content.strip()}\n")
                    
        return "\n\n".join(results) if results else "Failed to extract web data."
    except Exception as e:
        return f"Web browsing failed: {e}"

# ==========================================
# 3. Security Config & API Keys
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ALLOWED_USER_ID_STR = os.environ.get("ALLOWED_USER_ID", "5984629521")
ALLOWED_USER_IDS = [int(x.strip()) for x in ALLOWED_USER_ID_STR.split(",") if x.strip()] if ALLOWED_USER_ID_STR else []

# NEW: Notion API Credentials
NOTION_API_KEY = os.environ.get("NOTION_API_KEY")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")

# ==========================================
# 4. Model Pools & Custom Personalization
# ==========================================
ROUTING_POOL = ["gemini/gemini-2.5-flash", "github/gpt-4o-mini", "groq/llama-3.1-8b-instant"]
RESEARCH_POOL = ["groq/llama-3.3-70b-versatile", "github/gpt-4o", "gemini/gemini-2.5-pro"]

MASTER_PERSONA = """You are my personal engineering co-pilot and mentor. 
Think of yourself as an extension of my own brain. 

Context about me:
- I am an Electronics and VLSI engineering student.
- I specialize in SystemVerilog, UVM, and RTL design.
- My goal is to secure a 12+ LPA verification role at a Tier-1 semiconductor company.
- I regularly use Linux environments (like Rocky Linux) and EDA tools (Cadence Innovus, Synopsys) for RTL-to-GDSII flows.
- I script in Tcl and Python to automate workflows.

How you must behave:
1. EXTREME BREVITY: Give the exact, direct answer immediately. No fluff, no rambling.
2. ONE QUESTION ONLY: You must strictly end your response with EXACTLY ONE relevant follow-up question to keep me focused. Never ask two questions.
3. ADAPT TO ME: Frame your answers through the lens of semiconductor engineering.
4. BE PRACTICAL: Default to Verilog, SystemVerilog, Tcl, or Python for code.
5. USE MEMORY: Refer back to earlier context seamlessly.
6. SYSTEM ADMIN: If the user asks to execute a shell command, output EXACTLY 'EXECUTE: <command>' on a new line."""

CLASSIFIER_PROMPT = "Reply EXACTLY with [SEARCH_REQUIRED] if the user asks for real-world data, news, or URLs. Reply EXACTLY with [RESEARCH_REQUIRED] if the user asks for technical/VLSI tasks or coding. Otherwise, chat normally."

# ==========================================
# 5. Core Engine
# ==========================================
async def call_with_failover(messages, model_pool, temperature=0.7, context=None, status_msg=None, phase_name=""):
    for model in model_pool:
        try:
            if status_msg and context:
                try: await context.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text=f"🤔 [{phase_name}] Consulting {model}...")
                except: pass
            response = await acompletion(model=model, messages=messages, temperature=temperature, timeout=12)
            return response.choices[0].message.content.strip(), model
        except Exception:
            pass
    return "All AI models failed.", "Error"

# ==========================================
# 6. Telegram Handlers
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USER_IDS: return
    await update.message.reply_text("🧠 **Cloud Node Online!**\nWeb Browsing, Linux Execution, and Neon Postgres Memory Banks are operational.", parse_mode='Markdown')

async def direct_shell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender_id = update.effective_user.id
    if sender_id not in ALLOWED_USER_IDS: return

    linux_command = " ".join(context.args).strip()
    status_msg = await update.message.reply_text(f"⏳ Executing: `{linux_command}`...")

    try:
        process = subprocess.run(linux_command, shell=True, capture_output=True, text=True, timeout=15)
        terminal_log = (process.stdout + process.stderr).strip() or "[Empty output buffer]"
        log_audit(sender_id, linux_command, "SUCCESS", terminal_log)
        
        bt = "```"
        formatted_text = f"🖥️ **Output:**\n\n{bt}bash\n{terminal_log[:3800]}\n{bt}"
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=status_msg.message_id, text=formatted_text, parse_mode='Markdown')
        save_chat_memory(sender_id, "user", f"I manually executed this shell command: `{linux_command}`. The output was:\n{terminal_log}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender_id = update.effective_user.id
    if sender_id not in ALLOWED_USER_IDS: return

    user_query = update.message.text
    status_msg = await update.message.reply_text("🤔 Thinking...")
    
    chat_history = get_chat_history(sender_id, limit=8)
    current_date = datetime.now().strftime("%A, %B %d, %Y")
    system_context = f"{MASTER_PERSONA}\n\n[System Note: Today is {current_date}, 2026.]"

    try:
        route_reply, _ = await call_with_failover([{"role": "system", "content": CLASSIFIER_PROMPT}, {"role": "user", "content": user_query}], ROUTING_POOL, 0.2)
        
        messages = [{"role": "system", "content": system_context}] + chat_history
        
        if "[SEARCH_REQUIRED]" in route_reply:
            await context.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text="🌐 Searching web for context...")
            live_data = web_browse(user_query)
            messages.append({"role": "user", "content": f"Use this scraped data to answer: \n{live_data}\n\nMy prompt: {user_query}"})
        else:
            messages.append({"role": "user", "content": user_query})

        final_reply, final_model = await call_with_failover(messages, RESEARCH_POOL, 0.6, context, status_msg, "Synthesizing")
        
        save_chat_memory(sender_id, "user", user_query)
        save_chat_memory(sender_id, "assistant", final_reply)

    except Exception as e:
        final_reply = f"❌ Error: {e}"

    if "EXECUTE:" in final_reply:
        command_to_run = ""
        for line in final_reply.split('\n'):
            if "EXECUTE:" in line:
                command_to_run = line.split("EXECUTE:", 1)[1].strip()
                break
        command_to_run = command_to_run.strip("`'\" ")
        
        if command_to_run:
            context.user_data['pending_command'] = command_to_run
            context.user_data['status_msg_id'] = status_msg.message_id
            
            keyboard = [[InlineKeyboardButton("✅ Run Command", callback_data="run_approved"),
                         InlineKeyboardButton("❌ Cancel", callback_data="run_rejected")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id, message_id=status_msg.message_id,
                text=f"🛡️ **Command Run Option**\n\nThe AI suggests executing this command:\n\n`{command_to_run}`\n\nDo you authorize execution?",
                reply_markup=reply_markup, parse_mode='Markdown'
            )
            return

    try: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg.message_id)
    except: pass

    for chunk in [final_reply[i:i+4000] for i in range(0, len(final_reply), 4000)]:
        try: await update.message.reply_text(chunk, parse_mode='Markdown')
        except: await update.message.reply_text(chunk)

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    sender_id = query.from_user.id
    if sender_id not in ALLOWED_USER_IDS: return

    await query.answer()
    choice = query.data
    command = context.user_data.get('pending_command')
    status_msg_id = query.message.message_id

    if choice == "run_rejected":
        log_audit(sender_id, command, "REJECTED_BY_USER", "User clicked Cancel")
        context.user_data.clear()
        await context.bot.edit_message_text(chat_id=query.message.chat_id, message_id=status_msg_id, text=f"❌ **Cancelled:** `{command}`")
        return

    if choice == "run_approved":
        await context.bot.edit_message_text(chat_id=query.message.chat_id, message_id=status_msg_id, text=f"⚙️ Running: `{command}`...")
        try:
            process = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=15)
            result_log = (process.stdout + process.stderr).strip()[:3800] or "[Empty output buffer]"
            log_audit(sender_id, command, "SUCCESS", result_log)
            
            bt = "```"
            formatted_text = f"🖥️ **Output:**\n\n{bt}bash\n{result_log}\n{bt}"
            await context.bot.edit_message_text(chat_id=query.message.chat_id, message_id=status_msg_id, text=formatted_text, parse_mode='Markdown')
            save_chat_memory(sender_id, "user", f"I executed your suggested command `{command}`. The output was:\n{result_log}")
            
        except Exception as err:
            log_audit(sender_id, command, "ERROR", str(err))
            await context.bot.edit_message_text(chat_id=query.message.chat_id, message_id=status_msg_id, text=f"❌ **Error:** {err}")
        context.user_data.clear()

async def alarm_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    await context.bot.send_message(job.chat_id, text=f"🔔 **SIMULATION REMINDER:** `{job.data}`", parse_mode='Markdown')

async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender_id = update.effective_user.id
    if sender_id not in ALLOWED_USER_IDS: return
    
    try:
        time_str = context.args[0]
        task = " ".join(context.args[1:])
        
        unit = time_str[-1].lower()
        value = int(time_str[:-1]) if unit in ['s', 'm', 'h'] else int(time_str)
        
        if unit == 'h': delay = value * 3600
        elif unit == 'm': delay = value * 60
        else: delay = value 
        
        context.job_queue.run_once(alarm_callback, delay, chat_id=update.effective_chat.id, data=task)
        await update.message.reply_text(f"⏳ Timer set for {time_str}. Monitoring: `{task}`", parse_mode='Markdown')
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: `/remind <time(s/m/h)> <task>`\nExample: `/remind 45m Check Innovus timing reports`", parse_mode='Markdown')

async def scan_eda_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender_id = update.effective_user.id
    if sender_id not in ALLOWED_USER_IDS: return

    if not context.args:
        await update.message.reply_text("Usage: `/scanlog <path_to_log_file>`", parse_mode='Markdown')
        return

    log_path = context.args[0]
    
    try:
        with open(log_path, 'r') as file:
            log_data = file.read()
            
        drc_match = re.search(r'Total (?:number of )?DRC violations\s*[:=]\s*(\d+)', log_data, re.IGNORECASE)
        setup_match = re.search(r'Setup (?:WNS|slack)\s*[:=]\s*([-\d\.]+)', log_data, re.IGNORECASE)
        hold_match = re.search(r'Hold (?:WNS|slack)\s*[:=]\s*([-\d\.]+)', log_data, re.IGNORECASE)
        
        drc = drc_match.group(1) if drc_match else "0"
        setup = setup_match.group(1) if setup_match else "0.000"
        hold = hold_match.group(1) if hold_match else "0.000"
        
        is_clean = (drc == "0" and float(setup) >= 0 and float(hold) >= 0)
        header = "✅ **RTL-to-GDSII CLEAN**" if is_clean else "⚠️ **VIOLATIONS DETECTED**"

        report = (
            f"{header}\n\n"
            f"📄 **File:** `{log_path.split('/')[-1]}`\n"
            f"🛠️ **DRC Violations:** `{drc}`\n"
            f"⏱️ **Setup WNS:** `{setup} ns`\n"
            f"⏱️ **Hold WNS:** `{hold} ns`"
        )
        await update.message.reply_text(report, parse_mode='Markdown')

    except FileNotFoundError:
        await update.message.reply_text(f"❌ File not found: `{log_path}`")
    except Exception as e:
        await update.message.reply_text(f"❌ Error parsing log: {e}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender_id = update.effective_user.id
    if sender_id not in ALLOWED_USER_IDS: return

    document = update.message.document
    file_name = document.file_name
    status_msg = await update.message.reply_text(f"📥 Downloading `{file_name}` to cloud container...")

    try:
        bot_file = await context.bot.get_file(document.file_id)
        save_path = f"/tmp/{file_name}"
        await bot_file.download_to_drive(save_path)
        
        success_msg = (
            f"✅ **File saved successfully!**\n\n"
            f"Run this command to analyze your routing report:\n"
            f"`/scanlog {save_path}`"
        )
        await context.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text=success_msg, parse_mode='Markdown')
        save_chat_memory(sender_id, "user", f"I uploaded a file named {file_name}.")
        
    except Exception as e:
        await context.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text=f"❌ Download failed: {e}")

async def log_to_notion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender_id = update.effective_user.id
    if sender_id not in ALLOWED_USER_IDS: return

    if not context.args:
        await update.message.reply_text("Usage: `/log <your verilog or tcl snippet>`", parse_mode='Markdown')
        return

    content = " ".join(context.args)
    status_msg = await update.message.reply_text("📝 Beaming to Notion...")

    if not NOTION_API_KEY or not NOTION_DATABASE_ID:
        await context.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text="❌ Error: Notion keys missing in Render.")
        return

    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    
    # This targets the default "Name" title column in a Notion Database
    data = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "Name": {
                "title": [{"text": {"content": content[:2000]}}]
            }
        }
    }

    try:
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=10) as response:
            await context.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text="✅ **Saved to Knowledge Base!**", parse_mode='Markdown')
    except Exception as e:
        await context.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text=f"❌ API Error: {e}")

# ==========================================
# 7. Render Keep-Alive Web Server
# ==========================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"VLSI Bot is alive and running!")

    def log_message(self, format, *args):
        pass 

def start_health_server():
    port = int(os.environ.get("PORT", 10000)) 
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

if __name__ == "__main__":
    init_db()
    threading.Thread(target=start_health_server, daemon=True).start()

    request_config = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(request_config).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("sh", direct_shell))
    app.add_handler(CommandHandler("remind", set_reminder))
    app.add_handler(CommandHandler("scanlog", scan_eda_log))
    app.add_handler(CommandHandler("log", log_to_notion))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    app.run_polling()
