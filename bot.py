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

# Expanded profanity list
PROFANITIES = [
    "damn", 
    "porn", "nsfw", "xxx", "onlyfans", 
    "child abuse", "cp", "pedophile", "pedo"
]

# Track admin messages: chat_id -> user_id -> list of (timestamp, message_id)
admin_messages = defaultdict(lambda: defaultdict(list))
SPAM_LIMIT = 5
SPAM_TIME_WINDOW = 3.0 # seconds

# Track stickers/gifs: chat_id -> user_id -> list of (timestamp, message_id)
media_messages = defaultdict(lambda: defaultdict(list))
MEDIA_SPAM_LIMIT = 20
MEDIA_TIME_WINDOW = 30 * 60.0 # 30 minutes in seconds

# Track global chats and username mappings
known_chats = set()
username_to_id = {}

START_TIME = time.time()

def contains_profanity(text: str) -> bool:
    if not text: return False
    text = text.lower()
    for profanity in PROFANITIES:
        if profanity in text: return True
    return False

from typing import Optional, Tuple

async def get_chat_from_link(link: str, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    """Try to resolve a t.me link or @username to a chat_id."""
    if link.startswith('http'):
        link = link.split('/')[-1]
    if not link.startswith('@'):
        link = f"@{link}"
    try:
        chat = await context.bot.get_chat(link)
        return chat.id
    except Exception:
        return None

async def check_user_name_and_ban(user: User, chat: Chat, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not user or not chat: return False
    if chat.type == 'private': return False # Dont ban people for their name in DMs
        
    full_name = f"{user.first_name or ''} {user.last_name or ''}"
    if contains_profanity(full_name):
        try:
            await context.bot.ban_chat_member(chat_id=chat.id, user_id=user.id, revoke_messages=True)
            return True
        except Exception:
            pass
    return False

async def is_user_admin(chat: Chat, user_id: int) -> bool:
    try:
        member = await chat.get_member(user_id)
        return member.status in ['administrator', 'creator']
    except Exception:
        return False

async def handle_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = update.chat_member
    if not result: return
    user = result.new_chat_member.user
    chat = update.effective_chat
    if chat and chat.type != 'private':
        known_chats.add(chat.id)
    if user.username:
        username_to_id[user.username.lower()] = user.id
    await check_user_name_and_ban(user, chat, context)
    
async def handle_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    message = update.message
    
    if not user or not chat or not message: return
    if chat.type != 'private': known_chats.add(chat.id)
    if user.username: username_to_id[user.username.lower()] = user.id

    # 1. Channel Ban (If a channel posts in the group, and it's not an automatic forward)
    if message.sender_chat and message.sender_chat.type == 'channel' and not message.is_automatic_forward:
        try:
            await message.delete()
            await context.bot.ban_chat_sender_chat(chat_id=chat.id, sender_chat_id=message.sender_chat.id)
        except Exception:
            pass
        return
        
    # Ignore DMs for moderation filters
    if chat.type == 'private': return

    # 2. Profanity Check in Name
    if await check_user_name_and_ban(user, chat, context):
        try: await message.delete()
        except Exception: pass
        return

    # 3. Join/Leave Message Deletion
    if message.new_chat_members or message.left_chat_member:
        try: await message.delete()
        except Exception: pass
        return

    is_admin = await is_user_admin(chat, user.id)

    # 4. Profanity in Text/Caption
    if contains_profanity(message.text or message.caption or ""):
        try:
            if not is_admin:
                # Banning with revoke_messages=True automatically deletes all their past and present messages in one blow.
                await context.bot.ban_chat_member(chat_id=chat.id, user_id=user.id, revoke_messages=True)
                logger.info(f"Banned user {user.id} and wiped history for profanity in text.")
            else:
                # If they are an admin, we only delete the offending message.
                await message.delete()
        except Exception as e:
            logger.error(f"Failed to handle profanity in text: {e}")
        return

    now = time.time()

    # 5. Admin Spam Protection
    if is_admin:
        recent = admin_messages[chat.id][user.id]
        recent = [(ts, mid) for ts, mid in recent if now - ts <= SPAM_TIME_WINDOW]
        recent.append((now, message.message_id))
        admin_messages[chat.id][user.id] = recent
        
        if len(recent) >= SPAM_LIMIT:
            try:
                message_ids_to_delete = [mid for _, mid in recent]
                try:
                    await context.bot.delete_messages(chat_id=chat.id, message_ids=message_ids_to_delete)
                except Exception as e:
                    logger.error(f"Failed to bulk-delete admin spam: {e}")
                
                warning = f"🛡️ *{user.first_name}, as a guardian of this realm, your voice carries weight.*\n_Please preserve the tranquility and refrain from flooding the chat._"
                await context.bot.send_message(chat_id=chat.id, text=warning, parse_mode="Markdown")
                admin_messages[chat.id][user.id] = []
            except Exception as e:
                logger.error(f"Failed to handle admin spam cycle: {e}")

    # 6. Sticker/GIF Spam Protection
    if message.sticker or message.animation:
        recent_media = media_messages[chat.id][user.id]
        recent_media = [(ts, mid) for ts, mid in recent_media if now - ts <= MEDIA_TIME_WINDOW]
        recent_media.append((now, message.message_id))
        media_messages[chat.id][user.id] = recent_media
        
        if len(recent_media) >= MEDIA_SPAM_LIMIT:
            try:
                message_ids_to_delete = [mid for _, mid in recent_media]
                try:
                    await context.bot.delete_messages(chat_id=chat.id, message_ids=message_ids_to_delete)
                except Exception as e:
                    logger.error(f"Failed to bulk-delete sticker/GIF spam: {e}")
                    
                warn_msg = f"⚠️ {user.first_name}, you have sent too many stickers/GIFs. They have been removed to prevent spam."
                await context.bot.send_message(chat_id=chat.id, text=warn_msg)
                media_messages[chat.id][user.id] = []
            except Exception: pass


# --- Moderation Commands ---

async def resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Tuple[Optional[int], Optional[str]]:
    if update.message.reply_to_message:
        user = update.message.reply_to_message.from_user
        return user.id, user.first_name
    
    args = [a for a in context.args if not (a.startswith('t.me/') or a.startswith('http') or (a.startswith('@') and "://" not in a and a[1:].lower() not in username_to_id))]
    if args:
        arg = args[0]
        if arg.startswith('@'):
            uname = arg[1:].lower()
            if uname in username_to_id: return username_to_id[uname], arg
        elif arg.lstrip('-').isdigit():
            return int(arg), f"ID:{arg}"
            
    # For DMs, the developer might just provide username without having replied
    for arg in context.args or []:
        if arg.startswith('@'):
            uname = arg[1:].lower()
            if uname in username_to_id:
                return username_to_id[uname], arg
                
    await update.message.reply_text("❌ You must reply to a user's message or provide their @username/ID.")
    return None, None

async def resolve_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    """Returns the chat ID to perform the action in."""
    if update.effective_chat.type != 'private':
        return update.effective_chat.id
    # If in DM, look for a group link in args
    for arg in context.args or []:
        if 't.me/' in arg or arg.startswith('http') or arg.startswith('@'):
            # Only treat as group if it's not the target user
            if arg.startswith('@') and arg[1:].lower() in username_to_id and len(context.args) == 1:
                continue # It's just the user
            cid = await get_chat_from_link(arg, context)
            if cid: return cid
    await update.message.reply_text("❌ When using this command in DMs, please provide a group link like t.me/GroupLink.")
    return None

async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    user_id = update.effective_user.id
    if user_id == DEVELOPER_ID: return True
    if not await is_user_admin(await context.bot.get_chat(chat_id), user_id):
        await update.message.reply_text("❌ You must be an admin to use this command.")
        return False
    return True

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = await resolve_chat(update, context)
    if not chat_id: return
    if not await require_admin(update, context, chat_id): return
    target_id, target_name = await resolve_target(update, context)
    if not target_id: return
    
    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=target_id)
        await update.message.reply_text(f"🔨 {target_name} has been permanently banned.")
    except Exception as e:
        await update.message.reply_text(f"Failed to ban: {e}")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = await resolve_chat(update, context)
    if not chat_id: return
    if not await require_admin(update, context, chat_id): return
    target_id, target_name = await resolve_target(update, context)
    if not target_id: return
    
    try:
        await context.bot.unban_chat_member(chat_id=chat_id, user_id=target_id, only_if_banned=True)
        await update.message.reply_text(f"🕊️ {target_name} has been unbanned.")
    except Exception as e:
        await update.message.reply_text(f"Failed to unban: {e}")

async def kick_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = await resolve_chat(update, context)
    if not chat_id: return
    if not await require_admin(update, context, chat_id): return
    target_id, target_name = await resolve_target(update, context)
    if not target_id: return
    
    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=target_id)
        await context.bot.unban_chat_member(chat_id=chat_id, user_id=target_id)
        await update.message.reply_text(f"👢 {target_name} has been kicked.")
    except Exception as e:
        await update.message.reply_text(f"Failed to kick: {e}")

async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = await resolve_chat(update, context)
    if not chat_id: return
    if not await require_admin(update, context, chat_id): return
    target_id, target_name = await resolve_target(update, context)
    if not target_id: return
    
    try:
        await context.bot.restrict_chat_member(chat_id=chat_id, user_id=target_id, permissions=ChatPermissions(can_send_messages=False))
        await update.message.reply_text(f"🔇 {target_name} has been muted.")
    except Exception as e:
        await update.message.reply_text(f"Failed to mute: {e}")

async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = await resolve_chat(update, context)
    if not chat_id: return
    if not await require_admin(update, context, chat_id): return
    target_id, target_name = await resolve_target(update, context)
    if not target_id: return
    
    try:
        perms = ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True)
        await context.bot.restrict_chat_member(chat_id=chat_id, user_id=target_id, permissions=perms)
        await update.message.reply_text(f"🔊 {target_name} has been unmuted.")
    except Exception as e:
        await update.message.reply_text(f"Failed to unmute: {e}")

async def deleteall_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = await resolve_chat(update, context)
    if not chat_id: return
    if not await require_admin(update, context, chat_id): return
    target_id, target_name = await resolve_target(update, context)
    if not target_id: return
    
    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=target_id, revoke_messages=True)
        await context.bot.unban_chat_member(chat_id=chat_id, user_id=target_id, only_if_banned=True)
        await update.message.reply_text(f"🧹 All messages from {target_name} wiped.")
    except Exception as e:
        await update.message.reply_text(f"Failed to delete all messages: {e}")

async def gban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != DEVELOPER_ID:
        await update.message.reply_text("❌ Strict Developer Command.")
        return
        
    target_id, target_name = await resolve_target(update, context)
    if not target_id: return
    
    banned = 0
    failed = 0
    status = await update.message.reply_text(f"Starting GBAN for {target_name}...")
    for cid in list(known_chats):
        try:
            await context.bot.ban_chat_member(chat_id=cid, user_id=target_id, revoke_messages=True)
            banned += 1
        except Exception:
            failed += 1
    await status.edit_text(f"🌍 GBAN Complete for {target_name}\n✅ Banned in {banned} groups\n❌ Failed in {failed} groups.")

async def sudo_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != DEVELOPER_ID: return
    uptime = time.time() - START_TIME
    await update.message.reply_text(f"🏓 Pong!\n⏳ Uptime: {uptime:.2f} seconds\n🛡️ Active in {len(known_chats)} tracked chats.")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🤖 Hello! I am online.\nEnsure I have Admin rights to manage messages and ban users.")

def main() -> None:
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN missing.")
        return

    application = Application.builder().token(BOT_TOKEN).build()
    
    # Sudo
    application.add_handler(CommandHandler("sudo", sudo_ping))

    # General commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("kick", kick_command))
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("unmute", unmute_command))
    application.add_handler(CommandHandler("deleteall", deleteall_command))
    application.add_handler(CommandHandler("gban", gban_command))

    # Profiles updates & Generic Messages
    application.add_handler(ChatMemberHandler(handle_chat_member_update, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_any_message), group=1)

    logger.info("Bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
