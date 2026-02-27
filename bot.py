import logging
import os
import time
import asyncio
from collections import defaultdict
from dotenv import load_dotenv
from telegram import Update, User, Chat, ChatPermissions
from telegram.ext import Application, ChatMemberHandler, MessageHandler, CommandHandler, filters, ContextTypes

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEVELOPER_ID = int(os.getenv("DEVELOPER_ID", 0))

# Expanded profanity list including severe violations
PROFANITIES = [ "porn", "nsfw", "xxx", "onlyfans", 
    "child abuse", "cp", "pedophile", "pedo"
]

# Track admin messages for spam protection mapping chat_id -> user_id -> list[timestamps]
admin_messages = defaultdict(lambda: defaultdict(list))
SPAM_LIMIT = 5
SPAM_TIME_WINDOW = 3.0 # seconds

# Track which groups the bot is in
known_chats = set()

async def check_user_and_ban(user: User, chat: Chat, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Checks a user's name for profanity and bans them if found. Returns True if banned."""
    if not user or not chat:
        return False
        
    first_name = user.first_name or ""
    last_name = user.last_name or ""
    full_name = f"{first_name} {last_name}".lower()

    for profanity in PROFANITIES:
        if profanity in full_name:
            logger.info(f"Profanity `{profanity}` detected in user {user.id} ({full_name}). Banning...")
            try:
                await context.bot.ban_chat_member(chat_id=chat.id, user_id=user.id)
                logger.info(f"Successfully banned {user.id} from {chat.id}")
                return True
            except Exception as e:
                logger.error(f"Failed to ban user {user.id}: {e}")
            break
    return False

async def is_user_admin(chat: Chat, user_id: int) -> bool:
    """Helper to check if a user is an admin in a chat."""
    try:
        member = await chat.get_member(user_id)
        return member.status in ['administrator', 'creator']
    except Exception:
        return False

async def handle_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Triggered when a member's status changes, including name changes (if bot is admin)."""
    result = update.chat_member
    if not result:
        return
        
    user = result.new_chat_member.user
    chat = update.effective_chat
    
    # Track the chat
    if chat:
        known_chats.add(chat.id)

    await check_user_and_ban(user, chat, context)
    
async def handle_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Triggered on every single message to check the sender's name instantly and track admin spam."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.message
    
    if not user or not chat or not message:
        return

    # Track chat
    known_chats.add(chat.id)
    
    # 1. Check for Profanity in Name
    if await check_user_and_ban(user, chat, context):
        try:
            await message.delete()
        except Exception as e:
            logger.error(f"Failed to delete message from banned user: {e}")
        return # User is banned, stop processing

    # 2. Admin Spam Protection
    if chat.type in ['group', 'supergroup']:
        if await is_user_admin(chat, user.id):
            now = time.time()
            user_msgs = admin_messages[chat.id][user.id]
            
            # Remove old messages outside the time window
            user_msgs = [ts for ts in user_msgs if now - ts <= SPAM_TIME_WINDOW]
            user_msgs.append(now)
            admin_messages[chat.id][user.id] = user_msgs
            
            if len(user_msgs) >= SPAM_LIMIT:
                # Spam detected
                try:
                    await message.delete()
                    warning_msg = "🛡️ *As a guardian of this realm, your voice carries weight.*\n_Please preserve the tranquility and refrain from flooding the chat._"
                    await context.bot.send_message(chat_id=chat.id, text=warning_msg, parse_mode="Markdown")
                    # Clear their history so we don't spam the warning on msg 6, 7, 8...
                    admin_messages[chat.id][user.id] = []
                except Exception as e:
                    logger.error(f"Failed to handle admin spam: {e}")

# --- Moderation Commands ---

async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Ensure the user is an admin answering to a message."""
    user = update.effective_user
    chat = update.effective_chat
    if not await is_user_admin(chat, user.id):
        await update.message.reply_text("❌ You must be an admin to use this command.")
        return False
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ You must reply to a user's message to use this command.")
        return False
    return True

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context): return
    target_user = update.message.reply_to_message.from_user
    chat = update.effective_chat
    try:
        await context.bot.ban_chat_member(chat_id=chat.id, user_id=target_user.id)
        await update.message.reply_text(f"🔨 {target_user.first_name} has been permanently banned.")
    except Exception as e:
        await update.message.reply_text(f"Failed to ban: {e}")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context): return
    target_user = update.message.reply_to_message.from_user
    chat = update.effective_chat
    try:
        await context.bot.unban_chat_member(chat_id=chat.id, user_id=target_user.id, only_if_banned=True)
        await update.message.reply_text(f"🕊️ {target_user.first_name} has been unbanned.")
    except Exception as e:
        await update.message.reply_text(f"Failed to unban: {e}")

async def kick_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context): return
    target_user = update.message.reply_to_message.from_user
    chat = update.effective_chat
    try:
        await context.bot.ban_chat_member(chat_id=chat.id, user_id=target_user.id)
        await context.bot.unban_chat_member(chat_id=chat.id, user_id=target_user.id) # Unbanning allows them to rejoin
        await update.message.reply_text(f"👢 {target_user.first_name} has been kicked from the group.")
    except Exception as e:
        await update.message.reply_text(f"Failed to kick: {e}")

async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context): return
    target_user = update.message.reply_to_message.from_user
    chat = update.effective_chat
    try:
        permissions = ChatPermissions(can_send_messages=False)
        await context.bot.restrict_chat_member(chat_id=chat.id, user_id=target_user.id, permissions=permissions)
        await update.message.reply_text(f"🔇 {target_user.first_name} has been muted.")
    except Exception as e:
        await update.message.reply_text(f"Failed to mute: {e}")

async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context): return
    target_user = update.message.reply_to_message.from_user
    chat = update.effective_chat
    try:
        # Default permissions to allow sending messages
        permissions = ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True
        )
        await context.bot.restrict_chat_member(chat_id=chat.id, user_id=target_user.id, permissions=permissions)
        await update.message.reply_text(f"🔊 {target_user.first_name} has been unmuted.")
    except Exception as e:
        await update.message.reply_text(f"Failed to unmute: {e}")

async def gban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user.id != DEVELOPER_ID:
        await update.message.reply_text("❌ This command is restricted to the bot developer.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ You must reply to a user's message to gban them.")
        return
        
    target_user = update.message.reply_to_message.from_user
    banned_count = 0
    failed_count = 0
    
    status_message = await update.message.reply_text(f"Starting global ban for {target_user.first_name}...")
    
    for chat_id in list(known_chats):
        try:
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=target_user.id)
            banned_count += 1
        except Exception:
            failed_count += 1
            
    await status_message.edit_text(f"🌍 GBAN Complete for {target_user.first_name}\n✅ Banned in {banned_count} groups\n❌ Failed in {failed_count} groups")


def main() -> None:
    """Start the bot."""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set in the environment or .env file.")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    # Commands
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("kick", kick_command))
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("unmute", unmute_command))
    application.add_handler(CommandHandler("gban", gban_command))

    # Profiles updates
    application.add_handler(ChatMemberHandler(handle_chat_member_update, ChatMemberHandler.CHAT_MEMBER))
    
    # Generic Messages (Profanity Check & Spam Protection)
    # Using group=1 so it processes AFTER the commands handled in default group 0
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_any_message), group=1)

    logger.info("Bot is starting... (Listening to all messages and member updates)")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
