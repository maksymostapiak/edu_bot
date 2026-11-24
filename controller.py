import os
from datetime import date, timedelta
from typing import Optional
import httpx
from fastapi import (
    FastAPI, Depends, Request, HTTPException,
    BackgroundTasks, UploadFile, File, Form
)
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import SessionLocal, init_db
from models import Notification
from business_models import (
    UserModel, ScheduleModel, AiConsultationModel, NotificationModel
)
from telegram_views import get_main_menu_keyboard, get_schedule_keyboard
import asyncio

app = FastAPI(title="Telegram Educational Bot Controller")

# --- Конфігурація Telegram Bot API ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Стан очікування вводу (для пошуку групи/викладача)
USER_STATES: dict[str, str] = {}


# --- Залежність БД ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.on_event("startup")
def on_startup():
    init_db()


# --- Відправка повідомлень у Telegram ---
async def send_telegram_message(
    chat_id: str,
    text: str,
    reply_markup: Optional[dict] = None,
    parse_mode: str = "HTML",
):
    """Відправка повідомлення (з опціональною клавіатурою) через Telegram Bot API."""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)


async def answer_callback_query(callback_query_id: str):
    """Прибирає індикатор завантаження на inline-кнопці після натискання."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.post(
            f"{TELEGRAM_API_URL}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id},
        )


# =====================================================================
# 1. КОНТРОЛЕР TELEGRAM WEBHOOK (Взаємодія зі студентами)
# =====================================================================

async def telegram_polling_loop():
    """Фонове опитування Telegram (Long Polling) замість вебхука."""
    offset = 0
    async with httpx.AsyncClient(timeout=35.0) as client:
        # Очищаємо старий вебхук перед запуском polling
        await client.post(f"{TELEGRAM_API_URL}/deleteWebhook")

        while True:
            try:
                resp = await client.get(
                    f"{TELEGRAM_API_URL}/getUpdates",
                    params={"offset": offset, "timeout": 30},
                )
                updates = resp.json().get("result", [])

                for data in updates:
                    offset = data["update_id"] + 1
                    chat_id = None
                    full_name = ""
                    user_input = ""

                    if "callback_query" in data:
                        cb = data["callback_query"]
                        chat_id = str(cb["message"]["chat"]["id"])
                        full_name = cb["from"].get("first_name", "")
                        user_input = cb.get("data", "").strip()
                        await answer_callback_query(cb["id"])
                    elif "message" in data and "text" in data["message"]:
                        msg = data["message"]
                        chat_id = str(msg["chat"]["id"])
                        full_name = msg["from"].get("first_name", "")
                        user_input = msg["text"].strip()

                    if chat_id and user_input:
                        db = SessionLocal()
                        try:
                            user = UserModel(db).get_or_create(viber_id=chat_id, full_name=full_name)
                            await route_telegram_message(db, user, user_input)
                        finally:
                            db.close()

            except Exception as e:
                print(f"Polling error: {e}")
                await asyncio.sleep(3)


@app.on_event("startup")
async def on_startup():
    init_db()
    asyncio.create_task(telegram_polling_loop())


async def route_telegram_message(db: Session, user, text: str):
    """Маршрутизація команд меню та текстових запитів студента."""
    chat_id = user.viber_id
    user_model = UserModel(db)
    schedule_model = ScheduleModel(db)
    ai_model = AiConsultationModel(db)

    # --- Обробка стану очікування вводу групи/викладача ---
    if USER_STATES.get(chat_id) == "awaiting_schedule_search" and not text.startswith(("back_", "/")):
        USER_STATES.pop(chat_id, None)
        data = await schedule_model.get_daily_schedule(text, date.today())
        lessons = "\n".join(data["lessons"]) or "Пар немає."
        user_model.add_favorite_schedule(user, target_name=text)
        await send_telegram_message(
            chat_id,
            f"📅 <b>Розклад для {text} ({data['date']}):</b>\n{lessons}\n\n⭐ Додано у Вибране.",
            reply_markup=get_schedule_keyboard(),
        )
        return

    # --- Навігація та меню ---
    if text.lower() in ("/start", "/menu", "меню", "back_to_main"):
        USER_STATES.pop(chat_id, None)
        await send_telegram_message(
            chat_id,
            "Вітаю! Я освітній бот університету. Оберіть розділ у меню або просто напишіть запитання:",
            reply_markup=get_main_menu_keyboard(),
        )

    elif text == "menu_schedule":
        await send_telegram_message(
            chat_id,
            f"Ваша поточна група: <b>{user.group_name}</b>. Оберіть період:",
            reply_markup=get_schedule_keyboard(),
        )

    elif text == "schedule_today":
        data = await schedule_model.get_daily_schedule(user.group_name, date.today())
        lessons = "\n".join(data["lessons"]) or "Пар немає."
        await send_telegram_message(
            chat_id,
            f"📅 <b>Розклад на сьогодні ({data['date']}):</b>\n{lessons}",
            reply_markup=get_schedule_keyboard(),
        )

    elif text == "schedule_tomorrow":
        data = await schedule_model.get_daily_schedule(user.group_name, date.today() + timedelta(days=1))
        lessons = "\n".join(data["lessons"]) or "Пар немає."
        await send_telegram_message(
            chat_id,
            f"📅 <b>Розклад на завтра ({data['date']}):</b>\n{lessons}",
            reply_markup=get_schedule_keyboard(),
        )

    elif text == "schedule_week":
        days = await schedule_model.get_weekly_schedule(user.group_name)
        formatted = "\n\n".join(
            f"📅 <b>{d['date']}:</b>\n" + ("\n".join(d["lessons"]) or "Пар немає")
            for d in days
        )
        await send_telegram_message(chat_id, formatted, reply_markup=get_schedule_keyboard())

    elif text == "schedule_search":
        USER_STATES[chat_id] = "awaiting_schedule_search"
        await send_telegram_message(chat_id, "🔍 Введіть назву групи (напр. <code>ІПЗ-31</code>) або прізвище викладача:")

    elif text == "menu_favorites":
        favs = user_model.get_favorites(user)
        msg = (
            "⭐ <b>Ваші вибрані розклади:</b>\n" + "\n".join(f"• {f.target_name}" for f in favs)
            if favs
            else "⭐ Список вибраного порожній. Скористайтеся пошуком у розділі «Розклад»."
        )
        await send_telegram_message(chat_id, msg, reply_markup=get_main_menu_keyboard())

    elif text == "menu_notifications":
        enabled = user_model.toggle_push_notifications(user)
        status = "Увімкнено ✅" if enabled else "Вимкнено ❌"
        await send_telegram_message(
            chat_id,
            f"🔔 Push-сповіщення: <b>{status}</b>",
            reply_markup=get_main_menu_keyboard(),
        )

    elif text == "menu_ai":
        await send_telegram_message(
            chat_id,
            "🤖 Режим ШІ-консультації активний. Напишіть будь-яке запитання — я знайду відповідь у базі знань університету.",
            reply_markup=get_main_menu_keyboard(),
        )

    # --- Будь-який інший текст: RAG-консультація через ШІ ---
    else:
        result = await ai_model.process_query(user, text)
        answer = result["answer"]
        if result["sources"]:
            answer += "\n\n📎 <b>Офіційні джерела:</b>\n" + "\n".join(result["sources"])
        await send_telegram_message(chat_id, answer, reply_markup=get_main_menu_keyboard())


# =====================================================================
# 2. КОНТРОЛЕР АДМІН-ПАНЕЛІ (REST API для admin.html)
# =====================================================================

class NotificationRequest(BaseModel):
    course: str = "all"
    target_group: Optional[str] = None
    category: str
    message_text: str


class ScheduleConfigRequest(BaseModel):
    api_endpoint: str
    cache_timeout: int


@app.get("/admin")
def serve_admin_panel():
    """Віддає сторінку адмін-панелі."""
    return FileResponse("admin.html")


async def broadcast_notification_task(notification_id: int, course: str):
    """Фонова задача масової розсилки через Telegram."""
    db = SessionLocal()
    try:
        notif = db.query(Notification).filter_by(id=notification_id).first()
        if not notif:
            return

        notif_model = NotificationModel(db)
        recipients = notif_model.get_recipients(notif, course=course)

        for student in recipients:
            await send_telegram_message(
                student.viber_id,
                f"🔔 <b>[{notif.category.upper()}]</b>\n{notif.message_text}",
            )

        notif_model.mark_as_sent(notif)
    finally:
        db.close()


@app.post("/api/admin/notifications")
async def create_notification(
    payload: NotificationRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Створення події та запуск асинхронної розсилки."""
    notif = NotificationModel(db).create_notification(
        message_text=payload.message_text,
        category=payload.category,
        target_group=payload.target_group,
    )
    background_tasks.add_task(broadcast_notification_task, notif.id, payload.course)
    return {"status": "queued", "notification_id": notif.id}


@app.put("/api/admin/schedule-config")
async def update_schedule_config(
    payload: ScheduleConfigRequest,
    db: Session = Depends(get_db),
):
    """Оновлення URL джерела розкладу та очищення старого кешу."""
    config = ScheduleModel.update_config(
        db=db,
        api_endpoint=payload.api_endpoint,
        cache_timeout=payload.cache_timeout,
    )
    return {"status": "updated", "config": config}


@app.post("/api/admin/knowledge-base")
async def upload_knowledge_document(
    file: UploadFile = File(...),
    source_url: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Завантаження PDF/TXT, розбиття на чанки та векторизація в PostgreSQL."""
    content_bytes = await file.read()
    try:
        chunks_saved = await AiConsultationModel(db).upload_document(
            filename=file.filename,
            content_bytes=content_bytes,
            source_url=source_url,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"status": "indexed", "filename": file.filename, "chunks_saved": chunks_saved}