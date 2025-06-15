import os
import json
import random
import subprocess
import asyncio
import base64
from pathlib import Path
from datetime import datetime, timedelta

import requests
import time
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

class FusionBrainAPI:
    def __init__(self, url, api_key, secret_key):
        # url ДОЛЖЕН заканчиваться на '/'
        self.URL = url
        self.AUTH_HEADERS = {
            'X-Key':    f'Key {api_key}',
            'X-Secret': f'Secret {secret_key}',
        }

    def get_pipeline(self):
        # полный путь: https://api-key.fusionbrain.ai/key/api/v1/pipelines
        resp = requests.get(self.URL + 'key/api/v1/pipelines', headers=self.AUTH_HEADERS)
        resp.raise_for_status()
        return resp.json()[0]['id']

    def generate(self, prompt, pipeline_id, images=1, width=512, height=512):
        params = {
            "type": "GENERATE",
            "numImages": images,
            "width": width,
            "height": height,
            "generateParams": {"query": prompt},
        }
        files = {
            "pipeline_id": (None, pipeline_id),
            "params":      (None, json.dumps(params), "application/json"),
        }
        url = self.URL + "key/api/v1/pipeline/run"
        resp = requests.post(url, headers=self.AUTH_HEADERS, files=files)
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            # Вот здесь мы распечатаем тело ответа от сервера FusionBrain
            print("FusionBrain generate() failed:")
            print("  URL:", url)
            print("  Status code:", resp.status_code)
            print("  Response body:", resp.text)
            raise
        return resp.json()["uuid"]

    def check_generation(self, uuid, attempts=20, delay=3):
        for _ in range(attempts):
            resp = requests.get(
                self.URL + f'key/api/v1/pipeline/status/{uuid}',
                headers=self.AUTH_HEADERS
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get('status') == 'DONE':
                return data['result']['files']
            time.sleep(delay)
        return []

# === Bot Config ===
BOT_TOKEN     = "7627856208:AAHF8ZwhuowoJxGUO-1mYeqjhSEWfvMDUDE"
ADMIN_ID      = 7552313011

APPROVED_FILE = Path("approved_users.json")
if not APPROVED_FILE.exists():
    APPROVED_FILE.write_text("[]")
def load_approved() -> set:
    return set(json.loads(APPROVED_FILE.read_text()))
def save_approved(s: set):
    APPROVED_FILE.write_text(json.dumps(list(s)))

# === Paths & Constants ===
SETTINGS_DIR  = Path("bot_settings")
STATS_FILE    = Path("bot_stats.json")
FFMPEG_BIN    = Path("ffmpeg/bin/ffmpeg.exe")
FFPROBE_BIN   = FFMPEG_BIN.parent / "ffprobe.exe"
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB

# Ensure persistence
SETTINGS_DIR.mkdir(exist_ok=True)
if not STATS_FILE.exists():
    STATS_FILE.write_text(json.dumps({}))

# === Conversation States ===
(
    AWAITING_IMG_PROMPT,
    AWAITING_IMG_COUNT,
    UPLOADING_VIDEO,
    UPLOADING_IMAGES,
    CONFIGURING,
    OFFSET_X_INPUT,
    OFFSET_Y_INPUT
) = range(7)

# === Stats Helpers ===
def load_stats() -> dict:
    return json.loads(STATS_FILE.read_text())

def save_stats(stats: dict):
    STATS_FILE.write_text(json.dumps(stats, indent=2))

async def approve_user(chat_id: int):
    stats = load_stats()
    user = stats.get(str(chat_id), {})
    user['approved'] = True
    stats[str(chat_id)] = user
    save_stats(stats)

async def update_user_stat(chat_id: int, key: str, delta: int = 1):
    stats = load_stats()
    user = stats.get(str(chat_id), {})
    user[key] = user.get(key, 0) + delta
    if 'first_use' not in user:
        user['first_use'] = datetime.utcnow().isoformat()
    stats[str(chat_id)] = user
    save_stats(stats)

# === Settings Helpers ===
async def load_chat_settings(chat_id: int) -> dict:
    path = SETTINGS_DIR / f"{chat_id}.json"
    defaults = {
        "video_file": None,
        "images": [],
        "alpha": 0,
        "img_scale": 150,
        "video_scale": 100,
        "offset_x": 0,
        "offset_y": 0,
        "aspect": "9:16",
        "fps": 30,
        "n": 1,
        "animate": False,
        "use_img_gen": False,
    }
    if path.exists():
        data = json.loads(path.read_text())
        for k, v in defaults.items():
            data.setdefault(k, v)
        return data
    return defaults

async def save_chat_settings(chat_id: int, settings: dict):
    (SETTINGS_DIR / f"{chat_id}.json").write_text(
        json.dumps(settings, ensure_ascii=False, indent=2)
    )

# === Main Menu ===
async def send_main_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    settings = await load_chat_settings(chat_id)
    gen_label = "🎨 Генерация: ✔️" if settings['use_img_gen'] else "🎨 Генерация: ❌"
    kb = [
        [InlineKeyboardButton(gen_label, callback_data="toggle_img_gen")],
        [
            InlineKeyboardButton("🎬 Видео", callback_data="upload_video"),
            InlineKeyboardButton("🖼️ Картинки", callback_data="upload_images")
        ],
        [
            InlineKeyboardButton("🔍 Предпросмотр", callback_data="preview"),
            InlineKeyboardButton("🚀 Старт", callback_data="start_process")
        ],
        [
            InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
            InlineKeyboardButton("📊 Статистика", callback_data="stats")
        ]
    ]
    if chat_id == ADMIN_ID:
        kb.append([InlineKeyboardButton("🛠️ Админ панель", callback_data="admin_panel")])
    await context.bot.send_message(chat_id, "✨ Главное меню:", reply_markup=InlineKeyboardMarkup(kb))

# === Handlers ===

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_chat.id

    # проверяем только approved_users.json
    if uid not in load_approved():
        now = datetime.utcnow()
        stats = load_stats().get(str(uid), {})
        last = stats.get('last_request')
        if last:
            last_dt = datetime.fromisoformat(last)
            if now - last_dt < timedelta(hours=1):
                await update.message.reply_text(
                    "⏳ Вы уже подавали заявку недавно, повторить можно через час."
                )
                return ConversationHandler.END

        # отмечаем время этой попытки
        stats['last_request'] = now.isoformat()
        all_stats = load_stats()
        all_stats[str(uid)] = stats
        save_stats(all_stats)

        # шлём админу кнопки
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Принять", callback_data=f"approve_{uid}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"decline_{uid}")
        ]])
        await context.bot.send_message(
            ADMIN_ID,
            f"Новый запрос от @{update.effective_user.username or uid}",
            reply_markup=kb
        )
        await update.message.reply_text(
            "🟣Заявка была отправлена Администратору @durovpickme. Ожидайте"
        )
        return ConversationHandler.END

    # если уже в списке approved_users.json
    await update.message.reply_text("✨ Добро пожаловать!")
    await update_user_stat(uid, 'sessions', 0)
    await send_main_menu(uid, context)
    return CONFIGURING

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_chat.id
    if uid not in load_approved():
        if update.callback_query:
            await update.callback_query.message.reply_text(
                "❌ Доступ забран администратором. Чтобы подать новую заявку, используйте /start"
            )
        else:
            await update.message.reply_text(
                "❌ Доступ забран администратором. Чтобы подать новую заявку, используйте /start"
            )
        return ConversationHandler.END

    query = update.callback_query
    await query.answer()
    chat_id  = query.message.chat.id
    settings = await load_chat_settings(chat_id)
    data     = query.data

    # Toggle image-generation mode
    if data == 'toggle_img_gen':
        settings['use_img_gen'] = not settings['use_img_gen']
        await save_chat_settings(chat_id, settings)

        # строим обновлённую клавиатуру главного меню
        gen_label = "🎨 Генерация: ✔️" if settings['use_img_gen'] else "🎨 Генерация: ❌"
        kb = [
            [InlineKeyboardButton(gen_label, callback_data="toggle_img_gen")],
            [
                InlineKeyboardButton("🎬 Видео", callback_data="upload_video"),
                InlineKeyboardButton("🖼️ Картинки", callback_data="upload_images")
            ],
            [
                InlineKeyboardButton("🔍 Предпросмотр", callback_data="preview"),
                InlineKeyboardButton("🚀 Старт", callback_data="start_process")
            ],
            [
                InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
                InlineKeyboardButton("📊 Статистика", callback_data="stats")
            ]
        ]
        if chat_id == ADMIN_ID:
            kb.append([InlineKeyboardButton("🛠️ Админ панель", callback_data="admin_panel")])

        # просто обновляем разметку на месте
        await query.edit_message_reply_markup(InlineKeyboardMarkup(kb))
        return CONFIGURING

    # Prompt for FusionBrain если включена генерация
    if data == 'upload_images' and settings['use_img_gen']:
        await context.bot.send_message(chat_id, '📋 Введите текстовый промт для генерации картинки:')
        return AWAITING_IMG_PROMPT

    # Обычные пункты меню
    if data == 'upload_video':
        await context.bot.send_message(chat_id, 'Отправьте видео (<20MB) или ссылку:')
        return UPLOADING_VIDEO
    if data == 'upload_images':
        await context.bot.send_message(chat_id, 'Отправьте картинки или ZIP:')
        return UPLOADING_IMAGES
    if data == 'preview':
        return await preview(update, context)
    if data == 'start_process':
        return await start_processing(update, context)
    if data == 'settings':
        return await settings_menu(query, settings)
    if data == 'stats':
        return await show_stats(update, context)
    if data == 'set_offx':
        await context.bot.send_message(chat_id, 'Введите смещение X:')
        return OFFSET_X_INPUT
    if data == 'set_offy':
        await context.bot.send_message(chat_id, 'Введите смещение Y:')
        return OFFSET_Y_INPUT
    if data == 'admin_panel' and chat_id == ADMIN_ID:
        return await admin_panel(update, context)
    if data.startswith('user_') and chat_id == ADMIN_ID:
        uid = int(data.split('_', 1)[1])
        return await user_stats(update, context, uid)
    if data == 'back_main':
        await query.edit_message_text('✨ Главное меню:')
        await send_main_menu(chat_id, context)
        return CONFIGURING

    # Подстройка настроек
    step = 5
    if data == 'alpha_plus':    settings['alpha'] = min(100, settings['alpha'] + step)
    elif data == 'alpha_minus': settings['alpha'] = max(0, settings['alpha'] - step)
    elif data == 'img_plus':    settings['img_scale'] = min(300, settings['img_scale'] + step)
    elif data == 'img_minus':   settings['img_scale'] = max(10, settings['img_scale'] - step)
    elif data == 'vid_plus':    settings['video_scale'] = min(300, settings['video_scale'] + step)
    elif data == 'vid_minus':   settings['video_scale'] = max(10, settings['video_scale'] - step)
    elif data == 'fps_plus':    settings['fps'] = min(60, settings['fps'] + 5)
    elif data == 'fps_minus':   settings['fps'] = max(1, settings['fps'] - 5)
    elif data == 'n_plus':      settings['n'] = min(10, settings['n'] + 1)
    elif data == 'n_minus':     settings['n'] = max(1, settings['n'] - 1)
    elif data in ('aspect_prev','aspect_next'):
        opts = ['9:16','16:9','4:3']; idx = opts.index(settings['aspect'])
        settings['aspect'] = opts[(idx + (1 if data=='aspect_next' else -1)) % len(opts)]
    elif data == 'animate_toggle':
        settings['animate'] = not settings['animate']

    await save_chat_settings(chat_id, settings)
    return await settings_menu(query, settings)

async def approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action, uid_str = query.data.split("_",1)
    uid = int(uid_str)
    if query.from_user.id != ADMIN_ID:
        return CONFIGURING

    approved = load_approved()
    if action == "approve":
        approved.add(uid)
        msg = "✅ Доступ выдан."
        notify = "🎉 Вам открыт доступ к уникализатору! Напишите /start"
    else:  # revoke
        approved.discard(uid)
        msg = "❌ Доступ забран."
        notify = "⚠️ Администратор отклонил ваш запрос. Доступ запрещён.\nЕсли есть вопрос, напишите разработчику:\n@durovpickme"
    save_approved(approved)

    # уведомить пользователя
    try:
        await context.bot.send_message(uid, notify)
    except:
        pass

    # обновить кнопку
    new_mark = '✅' if action=='revoke' else '❌'
    new_action = 'revoke' if action!='revoke' else 'approve'
    kb = [[InlineKeyboardButton(f"{new_mark} {uid}", callback_data=f"{new_action}_{uid}")]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))
    return CONFIGURING

async def settings_menu(query, settings: dict) -> int:
    kb = [
        [
            InlineKeyboardButton(f"Прозрачность: {settings['alpha']}%", callback_data='alpha_minus'),
            InlineKeyboardButton('➕', callback_data='alpha_plus')
        ],
        [
            InlineKeyboardButton(f"Изобр. масштаб: {settings['img_scale']}%", callback_data='img_minus'),
            InlineKeyboardButton('➕', callback_data='img_plus')
        ],
        [
            InlineKeyboardButton(f"Видео масштаб: {settings['video_scale']}%", callback_data='vid_minus'),
            InlineKeyboardButton('➕', callback_data='vid_plus')
        ],
        [
            InlineKeyboardButton(f"FPS: {settings['fps']}", callback_data='fps_minus'),
            InlineKeyboardButton('➕', callback_data='fps_plus')
        ],
        [
            InlineKeyboardButton(f"Вариантов: {settings['n']}", callback_data='n_minus'),
            InlineKeyboardButton('➕', callback_data='n_plus')
        ],
        [
            InlineKeyboardButton(f"Aspect: {settings['aspect']} ←", callback_data='aspect_prev'),
            InlineKeyboardButton('→', callback_data='aspect_next')
        ],
        [
            InlineKeyboardButton(f"Анимация: {'✔' if settings['animate'] else '✖'}", callback_data='animate_toggle')
        ],
        [
            InlineKeyboardButton(f"Сдвиг X: {settings['offset_x']}", callback_data='set_offx'),
            InlineKeyboardButton(f"Сдвиг Y: {settings['offset_y']}", callback_data='set_offy')
        ],
        [
            InlineKeyboardButton('🔙 Назад', callback_data='back_main')
        ],
    ]
    await query.edit_message_text('⚙️ Настройки:', reply_markup=InlineKeyboardMarkup(kb))
    return CONFIGURING

async def upload_video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_chat.id
    if uid not in load_approved():
        await update.message.reply_text(
            "❌ Доступ забран администратором. Чтобы подать новую заявку, используйте /start"
        )
        return ConversationHandler.END

    # привязываем chat_id
    chat_id = uid

    text  = update.message.text or ''
    media = update.message.video or update.message.document

    if text.startswith('http'):
        resp = requests.get(text, stream=True); resp.raise_for_status()
        suf  = Path(text).suffix or '.mp4'
        p    = SETTINGS_DIR / f"{chat_id}_video{suf}"
        with open(p, 'wb') as f:
            for chunk in resp.iter_content(1024*1024):
                f.write(chunk)
        s = await load_chat_settings(chat_id)
        s['video_file'] = str(p)
        await save_chat_settings(chat_id, s)
        await context.bot.send_message(chat_id, '✅ URL видео сохранено.')
        await send_main_menu(chat_id, context)
        return CONFIGURING

    if not media or (media.file_size or 0) > MAX_FILE_SIZE:
        await context.bot.send_message(chat_id, '❌ Видео >20MB.')
        return UPLOADING_VIDEO

    fo  = await media.get_file()
    suf = Path(fo.file_path).suffix
    p   = SETTINGS_DIR / f"{chat_id}_video{suf}"
    await fo.download_to_drive(str(p))
    s = await load_chat_settings(chat_id)
    s['video_file'] = str(p)
    await save_chat_settings(chat_id, s)
    await context.bot.send_message(chat_id, '✅ Видео сохранено.')
    await send_main_menu(chat_id, context)
    return CONFIGURING

async def upload_images_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id  = update.effective_chat.id
    doc      = update.message.document
    imgs_dir = SETTINGS_DIR / f"{chat_id}_imgs"; imgs_dir.mkdir(exist_ok=True)
    if not doc:
        await context.bot.send_message(chat_id, '❌ Ошибка: нет файла')
        return UPLOADING_IMAGES
    fo = await doc.get_file(); fp = imgs_dir / doc.file_name
    await fo.download_to_drive(str(fp))
    if fp.suffix.lower() == '.zip':
        subprocess.run(['unzip','-o',str(fp),'-d',str(imgs_dir)]); fp.unlink()
    imgs = [str(p) for p in imgs_dir.iterdir() if p.suffix.lower() in ('.jpg','.jpeg','.png')]
    s    = await load_chat_settings(chat_id); s['images'] = imgs
    await save_chat_settings(chat_id, s)
    await context.bot.send_message(chat_id, f'✅ {len(imgs)} изображений загружено.')
    await send_main_menu(chat_id, context)
    return CONFIGURING

async def offset_x_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    try:
        x = int(update.message.text)
    except:
        await context.bot.send_message(chat_id, '❌ Введите число')
        return OFFSET_X_INPUT
    s             = await load_chat_settings(chat_id)
    s['offset_x'] = x
    await save_chat_settings(chat_id, s)
    await context.bot.send_message(chat_id, f'Сдвиг X: {x}')
    await send_main_menu(chat_id, context)
    return CONFIGURING

async def offset_y_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    try:
        y = int(update.message.text)
    except:
        await context.bot.send_message(chat_id, '❌ Введите число')
        return OFFSET_Y_INPUT
    s             = await load_chat_settings(chat_id)
    s['offset_y'] = y
    await save_chat_settings(chat_id, s)
    await context.bot.send_message(chat_id, f'Сдвиг Y: {y}')
    await send_main_menu(chat_id, context)
    return CONFIGURING

async def preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query   = update.callback_query; await query.answer()
    chat_id = query.message.chat.id
    s       = await load_chat_settings(chat_id)
    if not s['video_file'] or not s['images']:
        await context.bot.send_message(chat_id, '❌ Загрузите видео и изображения.')
        await send_main_menu(chat_id, context)
        return CONFIGURING

    w, h = (1080,1920) if s['aspect']=='9:16' else ((1920,1080) if s['aspect']=='16:9' else (1024,768))
    alpha = 1 - s['alpha']/100
    img_w, img_h = int(w*s['img_scale']/100), int(h*s['img_scale']/100)
    vid_w = int(w*s['video_scale']/100)
    off_x, off_y = s['offset_x'], s['offset_y']
    dur = 0
    if s['animate']:
        try:
            out = subprocess.check_output([
                str(FFPROBE_BIN), '-v','error','-show_entries','format=duration',
                '-of','default=noprint_wrappers=1:nokey=1', s['video_file']
            ])
            dur = float(out)
        except:
            dur = 0
    move = (f"x='(main_w-overlay_w)*t/{dur}':y='(main_h-overlay_h)*t/{dur}'" if s['animate'] and dur>0
            else "x='(main_w-overlay_w)/2':y='(main_h-overlay_h)/2'")
    preview_file = SETTINGS_DIR / f"{chat_id}_preview.png"
    cmd = [
        str(FFMPEG_BIN), '-y','-f','lavfi','-i',f"color=black:s={w}x{h}",
        '-i',s['images'][0],'-i',s['video_file'],
        '-filter_complex',
        (f"[1:v]scale={img_w}:{img_h},format=rgba[img];"
         f"[2:v]scale={vid_w}:-1,format=rgba,colorchannelmixer=aa={alpha}[vid];"
         f"[0:v][img]overlay={move}[tmp];"
         f"[tmp][vid]overlay=x='(main_w-overlay_w)/2+{off_x}':"
         f"y='(main_h-overlay_h)/2+{off_y}':shortest=1"),
        '-frames:v','1',str(preview_file)
    ]
    subprocess.run(cmd, check=True)
    await context.bot.send_photo(chat_id, open(preview_file,'rb'))
    await send_main_menu(chat_id, context)
    return CONFIGURING

async def generate_image_with_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    # 1) Проверяем число
    try:
        n = int(text)
        if not (1 <= n <= 10):
            raise ValueError()
    except ValueError:
        await context.bot.send_message(chat_id, "❌ Введите целое число от 1 до 10.")
        return AWAITING_IMG_COUNT

    prompt = context.user_data.get('img_prompt')
    if not prompt:
        await context.bot.send_message(chat_id, "❌ Пропт не найден. Начните заново (/start).")
        return CONFIGURING

    await context.bot.send_message(chat_id, f"⏳ Генерирую {n} изображений…")

    api = FusionBrainAPI(
        url="https://api-key.fusionbrain.ai/",
        api_key="F2566ABF386C604F384B976F56D76D40",
        secret_key="056DA47E786A5A5550062476E5EAF582"
    )

    imgs_dir = SETTINGS_DIR / f"{chat_id}_gen_imgs"
    imgs_dir.mkdir(exist_ok=True)
    paths = []

    try:
        pipeline_id = api.get_pipeline()

        for i in range(1, n + 1):
            # 2) Отдельный запрос для каждого изображения
            job_uuid = api.generate(prompt, pipeline_id, images=1, width=512, height=512)
            files    = await asyncio.to_thread(api.check_generation, job_uuid)
            if not files:
                await context.bot.send_message(chat_id, f"❌ Таймаут на изображении {i}.")
                continue

            # FusionBrain возвращает ровно один файл
            item = files[0]

            # скачиваем URL или декодируем Base64
            if item.startswith("http"):
                resp = await asyncio.to_thread(requests.get, item)
                resp.raise_for_status()
                img_bytes = resp.content
            else:
                b64 = item.split("base64,")[-1]
                img_bytes = base64.b64decode(b64)

            # сохраняем и отправляем
            p = imgs_dir / f"gen_{i}.png"
            p.write_bytes(img_bytes)
            paths.append(str(p))
            await context.bot.send_photo(chat_id, img_bytes)

    except Exception as e:
        msg = str(e)
        if len(msg) > 300:
            msg = msg[:300] + "…"
        await context.bot.send_message(chat_id, f"❌ Ошибка генерации: {msg}")
        return CONFIGURING

    # 3) Обновляем settings и чистим временные данные
    settings = await load_chat_settings(chat_id)
    settings['images'] = paths
    await save_chat_settings(chat_id, settings)
    context.user_data.pop('img_prompt', None)

    # 4) Возвращаемся в меню
    await send_main_menu(chat_id, context)
    return CONFIGURING

async def generate_image_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_chat.id
    if uid not in load_approved():
        await update.message.reply_text(
            "❌ Доступ забран администратором. Чтобы подать новую заявку, используйте /start"
        )
        return ConversationHandler.END

    prompt = update.message.text.strip()
    context.user_data['img_prompt'] = prompt
    await context.bot.send_message(
        uid,
        f"Промт сохранён:\n«{prompt}»\n\nСколько изображений сгенерировать? (введите число)"
    )
    return AWAITING_IMG_COUNT

async def start_processing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_chat.id
    if uid not in load_approved():
        await update.message.reply_text(
            "❌ Доступ забран администратором. Чтобы подать новую заявку, используйте /start"
        )
        return ConversationHandler.END
        
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id

    # сразу уведомляем пользователя, что процесс начался
    await context.bot.send_message(chat_id, "🚀 Начинаю уникализацию видео и изображений…")
    await context.bot.send_sticker(chat_id, "CAACAgIAAxkBAAEP2HFoTL2pTv0kLymmpnljn_CIoMQ25AACCU8AAjJq0EklRJJ0CnXAtTYE")  # <-- замените на свой file_id

    # грузим настройки
    s = await load_chat_settings(chat_id)
    if not s['video_file'] or not s['images']:
        await context.bot.send_message(chat_id, '❌ Загрузите видео и изображения.')
        await send_main_menu(chat_id, context)
        return CONFIGURING

    # параметры
    w, h = (1080,1920) if s['aspect']=='9:16' else ((1920,1080) if s['aspect']=='16:9' else (1024,768))
    out_dir = SETTINGS_DIR / f"{chat_id}_results"
    out_dir.mkdir(exist_ok=True)

    # считаем новую сессию
    await update_user_stat(chat_id, 'sessions', 1)

    # сам цикл рендеринга
    for i in range(1, s['n'] + 1):
        img_path = random.choice(s['images'])
        out_file = out_dir / f"uniq_{i}_{Path(s['video_file']).name}"
        alpha    = 1 - s['alpha'] / 100
        off_x, off_y = s['offset_x'], s['offset_y']
        img_w, img_h = int(w * s['img_scale'] / 100), int(h * s['img_scale'] / 100)
        vid_w = int(w * s['video_scale'] / 100)
        dur = 0
        if s['animate']:
            try:
                out = subprocess.check_output([
                    str(FFPROBE_BIN), '-v', 'error', '-show_entries', 'format=duration',
                    '-of', 'default=noprint_wrappers=1:nokey=1', s['video_file']
                ])
                dur = float(out)
            except:
                dur = 0
        move = (
            f"x='(main_w-overlay_w)*t/{dur}':y='(main_h-overlay_h)*t/{dur}'"
            if s['animate'] and dur > 0
            else "x='(main_w-overlay_w)/2':y='(main_h-overlay_h)/2'"
        )

        cmd = [
            str(FFMPEG_BIN), '-y', '-f', 'lavfi', '-i', f"color=black:s={w}x{h}",
            '-i', img_path, '-i', s['video_file'], '-filter_complex',
            (
                f"[1:v]scale={img_w}:{img_h},format=rgba[img];"
                f"[2:v]scale={vid_w}:-1,format=rgba,colorchannelmixer=aa={alpha}[vid];"
                f"[0:v][img]overlay={move}[tmp];"
                f"[tmp][vid]overlay=x='(main_w-overlay_w)/2+{off_x}':"
                f"y='(main_h-overlay_h)/2+{off_y}':shortest=1"
            ),
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '20',
            '-r', str(s['fps']), '-c:a', 'copy', str(out_file)
        ]
        subprocess.run(cmd, check=True)

        # отправляем готовый файл
        await context.bot.send_document(chat_id, open(out_file, 'rb'))
        await update_user_stat(chat_id, 'processed', 1)

    # по окончании
    await context.bot.send_message(chat_id, '✅ Уникализация завершена.')
    await send_main_menu(chat_id, context)
    return CONFIGURING

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid   = update.effective_chat.id
    st    = load_stats().get(str(uid),{})
    first = st.get('first_use','-'); ses = st.get('sessions',0); proc = st.get('processed',0)
    text  = f"📊 Статистика:\n- Зарегистрирован: {first}\n- Сессий: {ses}\n- Видео: {proc}"
    await context.bot.send_message(uid,text)
    await send_main_menu(uid,context)
    return CONFIGURING

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    stats = load_stats()
    approved = load_approved()
    kb = []
    for uid_str in stats:
        uid = int(uid_str)
        try:
            ch = await context.bot.get_chat(uid)
            name = " ".join(filter(None, (ch.first_name, ch.last_name)))
        except:
            name = str(uid)
        mark = '✅' if uid in approved else '❌'
        kb.append([InlineKeyboardButton(f"{mark} {name}", callback_data=f"user_{uid}")])
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data="back_main")])
    await query.edit_message_text("🛠️ Админ панель:", reply_markup=InlineKeyboardMarkup(kb))
    return CONFIGURING

async def user_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_chat.id
    if uid not in load_approved():
        await update.message.reply_text(
            "❌ Доступ забран администратором. Чтобы подать новую заявку, используйте /start"
        )
        return ConversationHandler.END
        
    query = update.callback_query
    await query.answer()
    uid = int(query.data.split("_",1)[1])

    st = load_stats().get(str(uid),{})
    first = st.get('first_use','-')
    ses   = st.get('sessions',0)
    proc  = st.get('processed',0)
    try:
        ch = await context.bot.get_chat(uid)
        name = " ".join(filter(None,(ch.first_name, ch.last_name)))
    except:
        name = str(uid)

    approved = uid in load_approved()
    action = 'revoke' if approved else 'approve'
    btn_text = "❌ Забрать доступ" if approved else "✅ Выдать доступ"
    kb = [
        [InlineKeyboardButton(btn_text, callback_data=f"{action}_{uid}")],
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_panel")],
    ]

    text = (
        f"Статистика {name} (id={uid}):\n"
        f"- Зарегистрирован: {first}\n"
        f"- Сессий: {ses}\n"
        f"- Видео: {proc}"
    )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return CONFIGURING

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cid = update.effective_chat.id
    await context.bot.send_message(cid,'❌ Отмена.')
    await send_main_menu(cid,context)
    return ConversationHandler.END

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ожидаем команду вида /approve_123456789
    m = re.match(r'^approve_(\d+)$', update.message.text or '')
    if not m or update.effective_user.id != ADMIN_ID:
        return
    uid = int(m.group(1))
    await approve_user(uid)
    await update.message.reply_text(f"✅ Пользователь {uid} одобрен.")
    # уведомляем юзера
    try:
        await context.bot.send_message(uid, "🎉 Вам открыт доступ! Напишите /start")
    except:
        pass

if __name__ == "__main__":
    from telegram.ext import (
        ApplicationBuilder,
        ConversationHandler,
        CommandHandler,
        MessageHandler,
        CallbackQueryHandler,
        filters,
    )

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # ——— 1) Глобальный ловец approve/decline/revoke (до ConversationHandler)
    app.add_handler(
        CallbackQueryHandler(
            approval_callback,
            pattern=r'^(approve|decline|revoke)_\d+$'
        )
    )

    # ——— 2) ConversationHandler для всего остального
    conv = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            AWAITING_IMG_PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, generate_image_handler)
            ],
            AWAITING_IMG_COUNT: [
                MessageHandler(filters.Regex(r'^[1-9]\d*$'), generate_image_with_count)
            ],
            UPLOADING_VIDEO: [
                MessageHandler(filters.ALL, upload_video_handler)
            ],
            UPLOADING_IMAGES: [
                MessageHandler(filters.ALL, upload_images_handler)
            ],
            OFFSET_X_INPUT: [
                MessageHandler(filters.Regex(r'^-?\d+$'), offset_x_input)
            ],
            OFFSET_Y_INPUT: [
                MessageHandler(filters.Regex(r'^-?\d+$'), offset_y_input)
            ],
            CONFIGURING: [
                # просмотр профиля пользователя в админке
                CallbackQueryHandler(user_stats, pattern=r'^user_\d+$'),
                # всё остальное меню (только button_callback):
                CallbackQueryHandler(button_callback),
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    app.run_polling()
