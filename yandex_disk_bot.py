# Telegram Bot for downloading photos and videos from Yandex Disk public links
# - Рекурсивный обход папок
# - Поддержка Local Bot API (файлы до 2 ГБ)
# - Режим: по одному файлу или ZIP-архив

import asyncio
import logging
import re
import zipfile
import io
from typing import Literal

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = "8838166051:AAHkiM2GTp4ZgIsdWcrOK44LGb9CT4klPog"

# Local Bot API Server (оставьте None для обычного API)
# Пример: "http://localhost:8081"
LOCAL_API_URL = None   # или "http://127.0.0.1:8081"

# Лимит размера файла (байты)
# Обычный API: 50 МБ, Local API: до 2 ГБ
MAX_FILE_SIZE = 2000 * 1024 * 1024 if LOCAL_API_URL else 49 * 1024 * 1024

# Режим по умолчанию: "single" или "zip"
DEFAULT_MODE: Literal["single", "zip"] = "single"

# ==============================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, base_url=LOCAL_API_URL) if LOCAL_API_URL else Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

YANDEX_LINK_PATTERN = re.compile(
    r'https?://(?:disk\.yandex\.(?:ru|com)|yadi\.sk|disk\.360\.yandex\.ru)/(?:[id]/)?[\w\-]+',
    re.IGNORECASE
)

API_BASE = "https://cloud-api.yandex.net/v1/disk/public/resources"


class UserMode(StatesGroup):
    mode = State()


async def get_resource_meta(session, public_url, path=None, limit=100, offset=0):
    params = {"public_key": public_url, "limit": limit, "offset": offset}
    if path:
        params["path"] = path
    try:
        async with session.get(API_BASE, params=params) as resp:
            if resp.status == 200:
                return await resp.json()
            logger.error(f"Meta error {resp.status}")
            return None
    except Exception as e:
        logger.error(f"Meta request failed: {e}")
        return None


async def get_download_link(session, public_url, path=None):
    params = {"public_key": public_url}
    if path:
        params["path"] = path
    try:
        async with session.get(f"{API_BASE}/download", params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("href")
            return None
    except Exception as e:
        logger.error(f"Download link failed: {e}")
        return None


def is_media(mime_type: str) -> bool:
    return mime_type and (mime_type.startswith("image/") or mime_type.startswith("video/"))


async def download_to_bytes(session, url: str) -> bytes | None:
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception as e:
        logger.error(f"Download failed: {e}")
    return None


async def process_file_single(message, session, public_url, path=None, name=None, mime_type=None, size=0):
    download_url = await get_download_link(session, public_url, path)
    if not download_url:
        await message.answer(f"❌ Не удалось получить ссылку: {name}")
        return

    caption = f"📁 {name}" if name else None

    if size and size > MAX_FILE_SIZE:
        await message.answer(
            f"⚠️ Файл слишком большой ({size // (1024*1024)} МБ).\n"
            f"Лимит: {MAX_FILE_SIZE // (1024*1024)} МБ\n"
            f"Ссылка: {download_url}",
            disable_web_page_preview=True
        )
        return

    try:
        if mime_type and mime_type.startswith("image/"):
            await message.answer_photo(photo=download_url, caption=caption)
        elif mime_type and mime_type.startswith("video/"):
            await message.answer_video(video=download_url, caption=caption)
        else:
            await message.answer_document(document=download_url, caption=caption)
    except Exception as e:
        logger.warning(f"Send by URL failed: {e}")
        content = await download_to_bytes(session, download_url)
        if not content:
            await message.answer(f"❌ Ошибка скачивания: {name}")
            return
        if len(content) > MAX_FILE_SIZE:
            await message.answer(f"⚠️ Файл слишком большой. Ссылка:\n{download_url}")
            return
        file = BufferedInputFile(content, filename=name or "file")
        if mime_type and mime_type.startswith("image/"):
            await message.answer_photo(photo=file, caption=caption)
        elif mime_type and mime_type.startswith("video/"):
            await message.answer_video(video=file, caption=caption)
        else:
            await message.answer_document(document=file, caption=caption)


async def collect_media_files(session, public_url, path=None, folder_name="", collected=None):
    """Собрать все медиафайлы рекурсивно (для ZIP)"""
    if collected is None:
        collected = []

    offset = 0
    limit = 100
    while True:
        meta = await get_resource_meta(session, public_url, path=path, limit=limit, offset=offset)
        if not meta:
            break
        items = meta.get("_embedded", {}).get("items", [])
        if not items:
            break

        for item in items:
            item_type = item.get("type")
            item_name = item.get("name", "")
            item_path = item.get("path")
            item_mime = item.get("mime_type")
            item_size = item.get("size", 0)

            if item_type == "file" and is_media(item_mime):
                display_name = f"{folder_name}/{item_name}".lstrip("/") if folder_name else item_name
                collected.append({
                    "path": item_path,
                    "name": display_name,
                    "mime": item_mime,
                    "size": item_size,
                    "public_url": public_url
                })
            elif item_type == "dir":
                sub_name = f"{folder_name}/{item_name}".lstrip("/") if folder_name else item_name
                await collect_media_files(session, public_url, item_path, sub_name, collected)

        if len(items) < limit:
            break
        offset += limit

    return collected


async def process_as_zip(message, session, public_url, folder_name: str):
    await message.answer(f"📦 Собираю файлы из папки «{folder_name}» в ZIP...")
    files = await collect_media_files(session, public_url, folder_name=folder_name)

    if not files:
        await message.answer("Не найдено фото и видео.")
        return

    total_size = sum(f["size"] for f in files)
    if total_size > MAX_FILE_SIZE:
        await message.answer(
            f"⚠️ Общий размер файлов слишком большой ({total_size // (1024*1024)} МБ).\n"
            f"Лимит: {MAX_FILE_SIZE // (1024*1024)} МБ"
        )
        return

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, f in enumerate(files, 1):
            await message.answer(f"⬇️ Скачиваю {i}/{len(files)}: {f['name']}")
            url = await get_download_link(session, f["public_url"], f["path"])
            if not url:
                continue
            content = await download_to_bytes(session, url)
            if content:
                # Сохраняем структуру папок в архиве
                zf.writestr(f["name"], content)
            await asyncio.sleep(0.3)

    zip_buffer.seek(0)
    zip_data = zip_buffer.read()
    zip_name = f"{folder_name or 'yandex_disk'}.zip"

    await message.answer_document(
        document=BufferedInputFile(zip_data, filename=zip_name),
        caption=f"📦 Архив с {len(files)} файлами"
    )


async def process_directory_single(message, session, public_url, path=None, folder_name="", counter=None):
    if counter is None:
        counter = [0]

    offset = 0
    limit = 100
    while True:
        meta = await get_resource_meta(session, public_url, path=path, limit=limit, offset=offset)
        if not meta:
            break
        items = meta.get("_embedded", {}).get("items", [])
        if not items:
            break

        for item in items:
            item_type = item.get("type")
            item_name = item.get("name", "")
            item_path = item.get("path")
            item_mime = item.get("mime_type")
            item_size = item.get("size", 0)

            if item_type == "file" and is_media(item_mime):
                counter[0] += 1
                display_name = f"{folder_name}/{item_name}".lstrip("/") if folder_name else item_name
                await process_file_single(
                    message, session, public_url,
                    path=item_path, name=display_name,
                    mime_type=item_mime, size=item_size
                )
                await asyncio.sleep(0.4)
            elif item_type == "dir":
                sub_name = f"{folder_name}/{item_name}".lstrip("/") if folder_name else item_name
                await process_directory_single(message, session, public_url, item_path, sub_name, counter)

        if len(items) < limit:
            break
        offset += limit

    return counter[0]


async def process_resource(message, session, public_url: str, mode: str):
    meta = await get_resource_meta(session, public_url)
    if not meta:
        await message.answer("❌ Не удалось получить информацию по ссылке.")
        return

    resource_type = meta.get("type")
    name = meta.get("name", "файл")

    if resource_type == "file":
        await message.answer(f"⬇️ Скачиваю: {name}")
        await process_file_single(
            message, session, public_url,
            name=name, mime_type=meta.get("mime_type"), size=meta.get("size", 0)
        )
    elif resource_type == "dir":
        if mode == "zip":
            await process_as_zip(message, session, public_url, name)
        else:
            await message.answer(f"📂 Папка: {name}. Отправляю файлы по одному...")
            counter = [0]
            total = await process_directory_single(message, session, public_url, folder_name=name, counter=counter)
            await message.answer(f"✅ Отправлено файлов: {total}")
    else:
        await message.answer("Неизвестный тип ресурса.")


def get_mode_keyboard(current: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📄 По одному файлу" + (" ✅" if current == "single" else ""),
                callback_data="mode_single"
            ),
            InlineKeyboardButton(
                text="📦 ZIP-архив" + (" ✅" if current == "zip" else ""),
                callback_data="mode_zip"
            )
        ]
    ])


@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.set_state(UserMode.mode)
    await state.update_data(mode=DEFAULT_MODE)
    mode = DEFAULT_MODE
    api_info = f"\n🔗 Local Bot API: включён (лимит {MAX_FILE_SIZE // (1024*1024)} МБ)" if LOCAL_API_URL else f"\n📏 Лимит: {MAX_FILE_SIZE // (1024*1024)} МБ"
    await message.answer(
        f"Привет! 👋{api_info}\n\n"
        "Отправь публичную ссылку на файл или папку с Яндекс.Диска.\n\n"
        "Выбери режим отправки:",
        reply_markup=get_mode_keyboard(mode)
    )


@dp.callback_query(F.data.startswith("mode_"))
async def set_mode(callback: CallbackQuery, state: FSMContext):
    mode = "zip" if callback.data == "mode_zip" else "single"
    await state.update_data(mode=mode)
    await callback.message.edit_reply_markup(reply_markup=get_mode_keyboard(mode))
    await callback.answer(f"Режим: {'ZIP-архив' if mode == 'zip' else 'по одному файлу'}")


@dp.message(Command("mode"))
async def cmd_mode(message: types.Message, state: FSMContext):
    data = await state.get_data()
    mode = data.get("mode", DEFAULT_MODE)
    await message.answer("Выбери режим:", reply_markup=get_mode_keyboard(mode))


@dp.message(F.text)
async def handle_text(message: types.Message, state: FSMContext):
    links = YANDEX_LINK_PATTERN.findall(message.text or "")
    if not links:
        await message.answer("Отправь ссылку на Яндекс.Диск.")
        return

    data = await state.get_data()
    mode = data.get("mode", DEFAULT_MODE)

    async with aiohttp.ClientSession() as session:
        for link in links:
            await message.answer(f"🔍 Обрабатываю:\n{link}\nРежим: {'ZIP' if mode == 'zip' else 'по одному'}")
            await process_resource(message, session, link, mode)


async def main():
    logger.info("Бот запущен...")
    if LOCAL_API_URL:
        logger.info(f"Используется Local Bot API: {LOCAL_API_URL}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
