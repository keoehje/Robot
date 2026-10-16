import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("downloader")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()
MAX_MB = int(os.environ.get("MAX_MB", "45"))
MAX_BYTES = MAX_MB * 1024 * 1024

# Deliberately allow common public video platforms only; arbitrary URLs can expose internal services.
ALLOWED_DOMAINS = {
    "youtube.com", "youtu.be", "youtube-nocookie.com", "music.youtube.com",
    "instagram.com", "instagr.am", "tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
    "twitter.com", "x.com", "vimeo.com", "dailymotion.com", "facebook.com",
    "fb.watch", "reddit.com", "redd.it", "twitch.tv", "streamable.com",
    "pinterest.com", "pin.it", "tumblr.com", "soundcloud.com"
}


def valid_public_video_url(text: str) -> bool:
    try:
        parsed = urlparse(text.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
            return False
        return any(host == domain or host.endswith("." + domain) for domain in ALLOWED_DOMAINS)
    except Exception:
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "سلام! 👋\n\nلینک عمومی ویدئو را از یکی از سایت‌های پشتیبانی‌شده بفرست تا دانلودش کنم.\n\n"
        "سایت‌ها: YouTube، Instagram، TikTok، X، Facebook، Reddit، Vimeo و چند سرویس دیگر.\n"
        f"حداکثر حجم فایل ارسالی: حدود {MAX_MB} مگابایت.\n\n"
        "نکته: لینک‌های خصوصی، محتوای نیازمند ورود، DRM و لینک‌های نامعتبر پشتیبانی نمی‌شوند. "
        "فقط محتوایی را دانلود کن که اجازهٔ دریافت و استفاده از آن را داری."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def download_with_ytdlp(url: str, folder: str) -> tuple[Path | None, str]:
    output_template = str(Path(folder) / "%(title).80s-%(id)s.%(ext)s")
    cmd = [
        "yt-dlp", "--no-playlist", "--no-warnings", "--restrict-filenames",
        "--socket-timeout", "15", "--retries", "1", "--fragment-retries", "1",
        "--max-filesize", f"{MAX_MB}M",
        "-f", "best[ext=mp4][filesize<45M]/best[ext=mp4][filesize_approx<45M]/best[ext=mp4]/best",
        "--merge-output-format", "mp4", "-o", output_template, url
    ]
    # Do not shell out through a shell; URL is passed as a distinct argument.
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=240)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return None, "زمان دانلود تمام شد؛ لطفاً بعداً دوباره امتحان کن."
        if proc.returncode != 0:
            err = (stderr or stdout).decode("utf-8", "ignore")[-1800:].lower()
            if "max-filesize" in err or "file is larger" in err:
                return None, f"حجم ویدئو بیشتر از محدودیت {MAX_MB} مگابایت است."
            if "unsupported url" in err:
                return None, "این لینک پشتیبانی نمی‌شود."
            log.warning("yt-dlp failed: %s", err[-700:])
            return None, "دانلود انجام نشد. ممکن است لینک خصوصی باشد، سایت محدودیت گذاشته باشد یا فرمت مناسبی پیدا نشود."
        files = [p for p in Path(folder).glob("*") if p.is_file() and p.suffix.lower() not in {".part", ".ytdl"}]
        if not files:
            return None, "فایل خروجی پیدا نشد؛ این لینک احتمالاً قابل دانلود نیست."
        file_path = max(files, key=lambda p: p.stat().st_size)
        if file_path.stat().st_size > MAX_BYTES:
            return None, f"فایل از حد {MAX_MB} مگابایت بزرگ‌تر است."
        return file_path, ""
    except FileNotFoundError:
        return None, "ابزار دانلود روی سرور نصب نیست؛ تنظیمات استقرار را بررسی کن."
    except Exception:
        log.exception("Unexpected download error")
        return None, "خطای غیرمنتظره‌ای هنگام دانلود رخ داد."


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return
    match = re.search(r"https?://\S+", message.text.strip())
    if not match:
        await message.reply_text("لطفاً یک لینک کامل با http یا https بفرست.")
        return
    url = match.group(0).rstrip(".,!?)\]}>")
    if not valid_public_video_url(url):
        await message.reply_text("این دامنه در فهرست سایت‌های پشتیبانی‌شده نیست. لینک عمومی یکی از سایت‌های معرفی‌شده را بفرست.")
        return

    status = await message.reply_text("⏳ لینک دریافت شد؛ در حال بررسی و دانلود…")
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    with tempfile.TemporaryDirectory(prefix="tgdl-") as temp_dir:
        file_path, error = await download_with_ytdlp(url, temp_dir)
        if error:
            await status.edit_text("❌ " + error)
            return
        try:
            await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_VIDEO)
            with file_path.open("rb") as media:
                if file_path.suffix.lower() in {".mp4", ".m4v", ".mov", ".webm"}:
                    await message.reply_video(video=media, caption="✅ دانلود شد")
                else:
                    await message.reply_document(document=media, caption="✅ فایل دانلود شد")
            await status.delete()
        except Exception as exc:
            log.warning("Telegram send failed: %s", exc)
            await status.edit_text("❌ دانلود انجام شد، اما ارسال فایل در تلگرام ناموفق بود. احتمالاً حجم فایل از محدودیت تلگرام بیشتر است.")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ADMIN_ID or not update.effective_user or str(update.effective_user.id) != ADMIN_ID:
        await update.effective_message.reply_text("این دستور فقط برای مدیر ربات است.")
        return
    await update.effective_message.reply_text(
        f"ربات فعال است.\nحداکثر حجم مجاز: {MAX_MB} MB\nسایت‌های مجاز: {len(ALLOWED_DOMAINS)} دامنه"
    )


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is required")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    log.info("Starting Telegram downloader bot with polling")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
