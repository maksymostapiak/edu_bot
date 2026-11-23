import os
from datetime import date, timedelta
from typing import Optional
import httpx
from fastapi import FastAPI, Depends, Request, BackgroundTasks, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from pypdf import PdfReader
import io

from database import SessionLocal, init_db
from models import (
    User, FavoriteSchedule, ChatMessage,
    KnowledgeChunk, Notification, ScheduleCache
)
from viber_views import get_main_menu_keyboard, get_schedule_keyboard

app = FastAPI(title="Viber Educational Bot Controller")


VIBER_AUTH_TOKEN = os.getenv("VIBER_AUTH_TOKEN", "YOUR_VIBER_BOT_TOKEN")
VIBER_SEND_URL = "https://chatapi.viber.com/pa/send_message"


SCHEDULE_CONFIG = {
    "api_endpoint": "https://api.university.edu.ua/v1/schedule",
    "cache_timeout_minutes": 30,
}


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




async def send_viber_message(receiver_id: str, text: str, keyboard: Optional[dict] = None):
    """Відправка сформованого View-повідомлення через Viber REST API."""
    payload = {
        "receiver": receiver_id,
        "type": "text",
        "text": text,
        "sender": {"name": "EduBot ЗВО"}
    }
    if keyboard:
        payload["keyboard"] = keyboard

    headers = {"X-Viber-Auth-Token": VIBER_AUTH_TOKEN}
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(VIBER_SEND_URL, json=payload, headers=headers)


async def get_embedding(text: str) -> list[float]:
    """
    Генерація вектора тексту через API (OpenAI / Gemini).
    Замініть на реальний HTTP-виклик вашого провайдера ембеддингів.
    """

    return [0.0] * 1536


async def call_llm(prompt: str, history: list[ChatMessage]) -> str:
    """
    Виклик зовнішньої LLM (OpenAI / Gemini API) з історією діалогу.
    Замініть на реальний запит через httpx або SDK.
    """
    return f"[Відповідь ШІ на основі бази знань]\n\nОброблено запит з урахуванням {len(history)} попередніх повідомлень."


async def fetch_schedule_with_cache(db: Session, target: str, target_date: date) -> str:
    """Отримання розкладу з кешуванням у PostgreSQL (ScheduleCache)."""
    cache_key = f"{target}_{target_date.isoformat()}"
    cached = db.query(ScheduleCache).filter_by(cache_key=cache_key).first()

    if cached:
        lessons = cached.schedule_data.get("lessons", [])
    else:

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    SCHEDULE_CONFIG["api_endpoint"],
                    params={"target": target, "date": target_date.isoformat()}
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:

            data = {"lessons": [f"1. 08:30 — Програмна інженерія ({target})", "2. 10:15 — Бази даних"]}

        db.add(ScheduleCache(cache_key=cache_key, schedule_data=data))
        db.commit()
        lessons = data.get("lessons", [])

    if not lessons:
        return f"📅 Розклад для {target} на {target_date}: пар немає."
    return f"📅 Розклад для {target} на {target_date}:\n" + "\n".join(lessons)


def get_or_create_user(db: Session, viber_id: str, name: Optional[str] = None) -> User:
    user = db.query(User).filter_by(viber_id=viber_id).first()
    if not user:
        user = User(
            viber_id=viber_id,
            full_name=name,
            group_name="ІПЗ-31",
            notification_categories=["schedule_changes", "deadlines"]
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


# =====================================================================
# 1. КОНТРОЛЕР VIBER WEBHOOK (Взаємодія зі студентами)
# =====================================================================

@app.post("/viber/webhook")
async def viber_webhook(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    event = data.get("event")


    if event == "webhook":
        return JSONResponse(status_code=200, content={"status": 0})


    if event in ("conversation_started", "subscribed"):
        sender = data.get("user", {})
        viber_id = sender.get("id")
        if viber_id:
            get_or_create_user(db, viber_id, sender.get("name"))
            await send_viber_message(
                viber_id,
                "Вітаю! Я освітній бот університету. Оберіть розділ у меню або просто напишіть запитання:",
                get_main_menu_keyboard()
            )
        return JSONResponse(status_code=200, content={"status": 0})


    if event == "message":
        sender = data.get("sender", {})
        viber_id = sender.get("id")
        text = data.get("message", {}).get("text", "").strip()

        user = get_or_create_user(db, viber_id, sender.get("name"))
        await route_user_message(db, user, text)

    return JSONResponse(status_code=200, content={"status": 0})


async def route_user_message(db: Session, user: User, text: str):
    """Маршрутизація команд меню та текстових запитів студента."""
    viber_id = user.viber_id


    if USER_STATES.get(viber_id) == "awaiting_schedule_search" and not text.startswith("back_"):
        USER_STATES.pop(viber_id, None)
        schedule_text = await fetch_schedule_with_cache(db, text, date.today())

        exists = db.query(FavoriteSchedule).filter_by(user_id=user.id, target_name=text).first()
        if not exists:
            db.add(FavoriteSchedule(user_id=user.id, target_type="group", target_name=text))
            db.commit()
        await send_viber_message(
            viber_id,
            f"{schedule_text}\n\n⭐ Додано «{text}» у Вибране.",
            get_schedule_keyboard()
        )
        return


    if text == "back_to_main":
        USER_STATES.pop(viber_id, None)
        await send_viber_message(viber_id, "Головне меню:", get_main_menu_keyboard())

    elif text == "menu_schedule":
        await send_viber_message(
            viber_id,
            f"Ваша поточна група: {user.group_name}. Оберіть період:",
            get_schedule_keyboard()
        )

    elif text == "schedule_today":
        msg = await fetch_schedule_with_cache(db, user.group_name, date.today())
        await send_viber_message(viber_id, msg, get_schedule_keyboard())

    elif text == "schedule_tomorrow":
        msg = await fetch_schedule_with_cache(db, user.group_name, date.today() + timedelta(days=1))
        await send_viber_message(viber_id, msg, get_schedule_keyboard())

    elif text == "schedule_week":
        days = [await fetch_schedule_with_cache(db, user.group_name, date.today() + timedelta(days=i)) for i in range(5)]
        await send_viber_message(viber_id, "\n\n".join(days), get_schedule_keyboard())

    elif text == "schedule_search":
        USER_STATES[viber_id] = "awaiting_schedule_search"
        await send_viber_message(viber_id, "Введіть назву групи (напр. ІПЗ-31) або прізвище викладача:")

    elif text == "menu_favorites":
        favs = db.query(FavoriteSchedule).filter_by(user_id=user.id).all()
        if not favs:
            msg = "⭐ У вас поки немає збережених розкладів. Скористайтеся пошуком у розділі «Розклад»."
        else:
            items = "\n".join(f"• {f.target_name}" for f in favs)
            msg = f"⭐ Ваші вибрані розклади:\n{items}"
        await send_viber_message(viber_id, msg, get_main_menu_keyboard())

    elif text == "menu_notifications":
        status = "Увімкнено ✅" if user.push_enabled else "Вимкнено ❌"
        cats = ", ".join(user.notification_categories or [])
        user.push_enabled = not user.push_enabled
        db.commit()
        new_status = "Увімкнено ✅" if user.push_enabled else "Вимкнено ❌"
        await send_viber_message(
            viber_id,
            f"🔔 Push-сповіщення: {new_status} (було: {status})\nАктивні категорії: {cats}",
            get_main_menu_keyboard()
        )

    elif text == "menu_ai":
        await send_viber_message(
            viber_id,
            "🤖 Режим ШІ-консультації активний. Просто напишіть ваше запитання (наприклад: «Як отримати довідку?» або «Правила поселення в гуртожиток»).",
            get_main_menu_keyboard()
        )

    else:
        await handle_ai_consultation(db, user, text)


async def handle_ai_consultation(db: Session, user: User, query: str):
    """RAG-пайплайн: пошук у pgvector -> збереження історії -> виклик LLM."""
    query_vector = await get_embedding(query)
    chunks = (
        db.query(KnowledgeChunk)
        .order_by(KnowledgeChunk.embedding.cosine_distance(query_vector))
        .limit(3)
        .all()
    )

    context_text = "\n---\n".join(c.chunk_text for c in chunks)
    sources = list({c.source_url for c in chunks if c.source_url})

    history = (
        db.query(ChatMessage)
        .filter_by(user_id=user.id)
        .order_by(ChatMessage.created_at.desc())
        .limit(5)
        .all()
    )
    history.reverse()

    prompt = f"Контекст з документів ЗВО:\n{context_text}\n\nЗапитання студента: {query}"
    answer = await call_llm(prompt, history)

    if sources:
        answer += "\n\n📎 Офіційні джерела:\n" + "\n".join(sources)

    db.add(ChatMessage(user_id=user.id, role="user", content=query))
    db.add(ChatMessage(user_id=user.id, role="assistant", content=answer))
    db.commit()

    await send_viber_message(user.viber_id, answer, get_main_menu_keyboard())


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
    """Фонова задача масової розсилки (щоб не блокувати HTTP-запит адмінки)."""
    db = SessionLocal()
    try:
        notif = db.query(Notification).filter_by(id=notification_id).first()
        if not notif:
            return

        query = db.query(User).filter(User.push_enabled.is_(True))
        if notif.target_group:
            query = query.filter(User.group_name == notif.target_group)
        elif course != "all":
            query = query.filter(User.course == int(course))

        recipients = query.all()
        for student in recipients:

            if not student.notification_categories or notif.category in student.notification_categories:
                await send_viber_message(
                    student.viber_id,
                    f"🔔 [{notif.category.upper()}]\n{notif.message_text}"
                )

        notif.is_sent = True
        db.commit()
    finally:
        db.close()


@app.post("/api/admin/notifications")
async def create_notification(
    payload: NotificationRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """Створення події та запуск асинхронної розсилки."""
    notif = Notification(
        target_group=payload.target_group,
        category=payload.category,
        message_text=payload.message_text,
        priority=2 if payload.category == "schedule_changes" else 1
    )
    db.add(notif)
    db.commit()
    db.refresh(notif)

    background_tasks.add_task(broadcast_notification_task, notif.id, payload.course)
    return {"status": "queued", "notification_id": notif.id}


@app.put("/api/admin/schedule-config")
async def update_schedule_config(payload: ScheduleConfigRequest, db: Session = Depends(get_db)):
    """Оновлення URL джерела розкладу та очищення старого кешу."""
    SCHEDULE_CONFIG["api_endpoint"] = payload.api_endpoint
    SCHEDULE_CONFIG["cache_timeout_minutes"] = payload.cache_timeout


    db.query(ScheduleCache).delete()
    db.commit()
    return {"status": "updated", "config": SCHEDULE_CONFIG}


@app.post("/api/admin/knowledge-base")
async def upload_knowledge_document(
    file: UploadFile = File(...),
    source_url: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    """Завантаження PDF/TXT, розбиття на чанки та векторизація в PostgreSQL."""
    content_bytes = await file.read()

    if file.filename.lower().endswith(".pdf"):
        reader = PdfReader(io.BytesIO(content_bytes))
        full_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    elif file.filename.lower().endswith(".txt"):
        full_text = content_bytes.decode("utf-8", errors="ignore")
    else:
        raise HTTPException(status_code=400, detail="Підтримуються лише PDF та TXT файли")

    chunk_size = 500
    chunks = [full_text[i:i + chunk_size].strip() for i in range(0, len(full_text), chunk_size)]
    chunks = [c for c in chunks if c]

    for chunk_text in chunks:
        vector = await get_embedding(chunk_text)
        db.add(KnowledgeChunk(
            document_name=file.filename,
            source_url=source_url,
            chunk_text=chunk_text,
            embedding=vector
        ))

    db.commit()
    return {"status": "indexed", "filename": file.filename, "chunks_saved": len(chunks)}