import logging
import asyncio
import os
import subprocess
import sqlite3
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
# 1. Logging & Database Setup
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

DB_FILE = "audit.db"

def init_db():
    """Initializes the enterprise audit logging database."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS command_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            user_id INTEGER,
            command TEXT,
            status TEXT,
            output TEXT
        )
    ''')
    conn.commit()
    conn.close()
    logging.info("✅ Audit Database Initialized.")

def log_audit(user_id, command, status, output):
    """Securely logs every command execution into the database."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute('''
        INSERT INTO command_audit (timestamp, user_id, command, status, output)
        VALUES (?, ?, ?, ?, ?)
    ''', (timestamp, user_id, command, status, output))
    conn.commit()
    conn.close()

# ==========================================
# 2. Security Config & API Keys
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ALLOWED_USER_ID_STR = os.environ.get("ALLOWED_USER_ID", "5984629521")

try:
    ALLOWED_USER_IDS = [int(x.strip()) for x in ALLOWED_USER_ID_STR.split(",") if x.strip()]
except Exception as parse_err:
    logging.error(f"Error parsing ALLOWED_USER_ID: {parse_err}.")
    ALLOWED_USER_IDS = []

if not TELEGRAM_TOKEN:
    raise ValueError("CRITICAL ERROR: TELEGRAM_TOKEN is missing from your .env configuration!")

# ==========================================
# 3. Ultimate 10-Model Failover Pools
# ==========================================
ROUTING_POOL = [
    "gemini/gemini-2.5-flash",
    "github/gpt-4o-mini",
    "groq/llama-3.1-8b-instant",
    "github/Phi-3.5-mini-instruct"
]

RESEARCH_POOL = [
    "groq/llama-3.3-70b-versatile",
    "github/gpt-4o",
    "gemini/gemini-2.5-pro",
    "github/Meta-Llama-3-70B-Instruct",
    "github/Mistral-large-2-instruct",
    "github/Command-r-plus"
]

MODEL_LABELS = {
    "gemini/gemini-2.5-flash": "Google Gemini 2.5 Flash ⚡",
    "github/gpt-4o-mini": "OpenAI GPT-4o-Mini 🤖",
    "groq/llama-3.1-8b-instant": "Groq Llama 3.1 (8B) 🏎️",
    "github/Phi-3.5-mini-instruct": "Microsoft Phi-3.5 Mini 🧭",
    "groq/llama-3.3-70b-versatile": "Groq Llama 3.3 (70B) 🧠",
    "github/gpt-4o": "OpenAI GPT-4o Gold 🏆",
    "gemini/gemini-2.5-pro": "Google Gemini 2.5 Pro 🔬",
    "github/Meta-Llama-3-70B-Instruct": "Meta Llama 3 (70B) 🛸",
    "github/Mistral-large-2-instruct": "Mistral Large 2 🪐",
    "github/Command-r-plus": "Cohere Command R+ 🛡️"
}

# ==========================================
# 4. System Prompts
# ==========================================
CLASSIFIER_PROMPT = (
    "You are a smart router and conversational assistant. Analyze the user's message. "
    "If the user is asking for system operations, terminal tasks, running commands, or gathering machine-specific data, "
    "or if the query requires deep technical explanation, detailed research, VLSI-specific analysis, expert coding, "
    "or complex logical reasoning, reply with EXACTLY the text: [RESEARCH_REQUIRED] "
    "Do not include any other text if research is required. "
    "Otherwise, answer the simple conversational query or greeting directly in a clear, brief, and friendly manner."
)

RESEARCH_SYSTEM_PROMPT = (
    "You are an expert research and system administration AI assistant. "
    "Provide highly accurate, thoroughly detailed, and technical explanations.\n\n"
    "CRITICAL COMMAND EXECUTION RULE:\n"
    "If the user asks you to perform a task on their Linux computer, check system settings, files, or run commands, "
    "and you need to execute a shell command to do so, output EXACTLY 'EXECUTE: <command>' on a new line with nothing else.\n"
    "Example: EXECUTE: free -m"
)

# ==========================================
# 5. Core Multi-Model Failover Request Engine
# ==========================================
async def call_with_failover(messages, model_pool, temperature=0.7, context=None, status_msg=None, phase_name=""):
    errors = []
    for model in model_pool:
        try:
            label = MODEL_LABELS.get(model, model)
            if status_msg and context:
                try:
                    await context.bot.edit_message_text(
                        chat_id=status_msg.chat_id,
                        message_id=status_msg.message_id,
                        text=f"🤔 [{phase_name}] Consulting {label}..."
                    )
                except Exception:
                    pass
            
            response = await acompletion(
                model=model,
                messages=messages,
                temperature=temperature,
                timeout=12
            )
            return response.choices[0].message.content.strip(), model
            
        except Exception as e:
            label = MODEL_LABELS.get(model, model)
            logging.error(f"Model failure warning: {label} failed during {phase_name}. Error: {e}")
            errors.append(f"{label}: {str(e)}")
            
            if status_msg and context:
                try:
                    await context.bot.edit_message_text(
                        chat_id=status_msg.chat_id,
                        message_id=status_msg.message_id,
                        text=f"⚠️ {label} limited/down. Attempting alternative engine..."
                    )
                    await asyncio.sleep(0.8)
                except Exception:
                    pass
                    
    raise RuntimeError("All available fallback models in the cluster have failed.\n" + "\n".join(errors))

# ==========================================
# 6. Telegram Handlers
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender_id = update.effective_user.id
    if sender_id not in ALLOWED_USER_IDS:
        return

    welcome_text = (
        "🤖 **Enterprise AI Console Online!**\n\n"
        "10-Model HA Cluster active. Auditing Database is online.\n\n"
        "• **Adaptive Text Chat:** Send any query.\n"
        "• **Direct Shell:** Type `/sh <command>` to run host system scripts."
    )
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def direct_shell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender_id = update.effective_user.id
    if sender_id not in ALLOWED_USER_IDS:
        return

    command_args = context.args
    if not command_args:
        await update.message.reply_text("⚠️ Specify a baseline command. Example: `/sh free -m`")
        return

    linux_command = " ".join(command_args).strip()
    status_msg = await update.message.reply_text(f"⏳ Executing command: `{linux_command}`...")

    try:
        process = subprocess.run(linux_command, shell=True, capture_output=True, text=True, timeout=15)
        output = process.stdout if process.stdout else ""
        errors = process.stderr if process.stderr else ""
        terminal_log = (output + errors).strip()

        if not terminal_log:
            terminal_log = "[Command executed successfully with an empty output buffer]"

        # Log to Database
        log_audit(sender_id, linux_command, "SUCCESS", terminal_log)

        bt = "```"
        final_response = f"🖥️ **Terminal Output:**\n\n{bt}bash\n{terminal_log[:3800]}\n{bt}"
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id, 
            message_id=status_msg.message_id, 
            text=final_response,
            parse_mode='Markdown'
        )
    except subprocess.TimeoutExpired:
        log_audit(sender_id, linux_command, "TIMEOUT", "Execution exceeded 15 seconds")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id, message_id=status_msg.message_id, 
            text="❌ **Timeout Error:** Command hit the 15-second process limit."
        )
    except Exception as shell_err:
        log_audit(sender_id, linux_command, "ERROR", str(shell_err))
        await update.message.reply_text(f"❌ Subprocess execution fault: {shell_err}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender_id = update.effective_user.id
    if sender_id not in ALLOWED_USER_IDS:
        return

    user_query = update.message.text
    status_msg = await update.message.reply_text("🤔 Analyzing query route mapping...")

    final_answer = ""
    try:
        classification_reply, classification_model = await call_with_failover(
            messages=[{"role": "system", "content": CLASSIFIER_PROMPT}, {"role": "user", "content": user_query}],
            model_pool=ROUTING_POOL,
            temperature=0.3,
            context=context,
            status_msg=status_msg,
            phase_name="Routing Architecture"
        )
        
        if "[RESEARCH_REQUIRED]" in classification_reply:
            research_reply, research_model = await call_with_failover(
                messages=[{"role": "system", "content": RESEARCH_SYSTEM_PROMPT}, {"role": "user", "content": user_query}],
                model_pool=RESEARCH_POOL,
                temperature=0.7,
                context=context,
                status_msg=status_msg,
                phase_name="Deep Research Engine"
            )
            engine_name = MODEL_LABELS.get(research_model, "Research AI")
            final_answer = f"🔬 **{engine_name}** 🔬\n\n{research_reply}"
        else:
            engine_name = MODEL_LABELS.get(classification_model, "Basic Assistant")
            final_answer = f"✨ **{engine_name}** ✨\n\n{classification_reply}"

    except Exception as general_failover_err:
        logging.error(f"All 10 models exhausted: {general_failover_err}")
        final_answer = "❌ **Global Cluster Outage:** All 10 models in the arrays failed."

    # Intercept Command Run Option
    if "EXECUTE:" in final_answer:
        command_to_run = ""
        for line in final_answer.split('\n'):
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

    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg.message_id)
    except Exception:
        pass

    max_chars = 4000
    chunks = [final_answer[i:i+max_chars] for i in range(0, len(final_answer), max_chars)]
    for chunk in chunks:
        try:
            await update.message.reply_text(chunk, parse_mode='Markdown')
        except Exception:
            await update.message.reply_text(f"⚠️ (Formatting Fallback)\n\n{chunk}")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    sender_id = query.from_user.id
    if sender_id not in ALLOWED_USER_IDS:
        return

    await query.answer()
    choice = query.data
    command = context.user_data.get('pending_command')
    status_msg_id = query.message.message_id

    if choice == "run_rejected":
        log_audit(sender_id, command, "REJECTED_BY_USER", "User clicked Cancel")
        context.user_data.clear()
        await context.bot.edit_message_text(
            chat_id=query.message.chat_id, message_id=status_msg_id,
            text=f"❌ **Execution Cancelled:** Suggestion to run `{command}` was rejected."
        )
        return

    if choice == "run_approved":
        await context.bot.edit_message_text(
            chat_id=query.message.chat_id, message_id=status_msg_id, text=f"⚙️ Running command: `{command}`..."
        )
        try:
            process = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=15)
            output = process.stdout if process.stdout else ""
            errors = process.stderr if process.stderr else ""
            result_log = (output + errors).strip()[:3800]
            if not result_log:
                result_log = "[Command completed with empty output buffer]"
            
            # Log to Database
            log_audit(sender_id, command, "SUCCESS", result_log)

            bt = "```"
            await context.bot.edit_message_text(
                chat_id=query.message.chat_id, message_id=status_msg_id,
                text=f"🖥️ **Terminal Output:**\n\n{bt}bash\n{result_log}\n{bt}", parse_mode='Markdown'
            )
        except subprocess.TimeoutExpired:
            log_audit(sender_id, command, "TIMEOUT", "Execution exceeded 15 seconds")
            await context.bot.edit_message_text(chat_id=query.message.chat_id, message_id=status_msg_id, text="❌ **Timeout Error:** Command exceeded 15 seconds.")
        except Exception as err:
            log_audit(sender_id, command, "ERROR", str(err))
            await context.bot.edit_message_text(chat_id=query.message.chat_id, message_id=status_msg_id, text=f"❌ **Execution Error:** {err}")
        
        context.user_data.clear()

if __name__ == "__main__":
    # Initialize the database on startup
    init_db()
    
    LAB_PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("http_proxy") or None
    request_config = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0, proxy=LAB_PROXY)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(request_config).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("sh", direct_shell))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()
