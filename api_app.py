from datetime import date, timedelta
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, init_db
from business_models import (
    UserModel, ScheduleModel, AiConsultationModel, NotificationModel
)

app = FastAPI(title="Educational Web ChatBot API")

# Тимчасовий стан очікування вводу (для пошуку групи/викладача)
USER_STATES: dict[str, str] = {}


@app.on_event("startup")
def on_startup():
    init_db()


# --- Схеми запитів (Pydantic) ---

class ChatMessageRequest(BaseModel):
    session_id: str
    text: Optional[str] = None
    action: Optional[str] = None


class NotificationRequest(BaseModel):
    target_group: Optional[str] = None
    category: str
    message_text: str


class ScheduleConfigRequest(BaseModel):
    api_endpoint: str
    cache_timeout: int


def serialize_user_state(db: Session, user):
    """Формує актуальний стан профілю та сповіщень для оновлення бічної панелі."""
    notifs = NotificationModel(db).get_user_notifications(user, limit=5)
    return {
        "user": {
            "group_name": user.group_name,
            "notifications_enabled": user.notifications_enabled,
        },
        "notifications": [
            {"category": n.category, "message_text": n.message_text}
            for n in notifs
        ],
    }


# =====================================================================
# 1. ЕНДПОІНТИ ВЕБЧАТУ
# =====================================================================

@app.get("/api/chat/init")
def init_chat(session_id: str, db: Session = Depends(get_db)):
    user = UserModel(db).get_or_create(session_id=session_id)
    history = AiConsultationModel(db).get_recent_history(user.id, limit=20)
    state = serialize_user_state(db, user)
    state["history"] = [{"role": m.role, "content": m.content} for m in history]
    return state


@app.post("/api/chat/message")
async def handle_chat_message(payload: ChatMessageRequest, db: Session = Depends(get_db)):
    user_model = UserModel(db)
    schedule_model = ScheduleModel(db)
    ai_model = AiConsultationModel(db)

    user = user_model.get_or_create(session_id=payload.session_id)
    sid = user.session_id
    action = payload.action
    text = (payload.text or "").strip()

    reply = ""
    sources = []

    # --- Обробка кнопок бічної панелі ---
    if action == "schedule_today":
        USER_STATES.pop(sid, None)
        data = await schedule_model.get_daily_schedule(user.group_name, date.today())
        lessons = "\n".join(data["lessons"]) or "Пар немає."
        reply = f"📅 Розклад на сьогодні ({data['date']}) для {user.group_name}:\n{lessons}"

    elif action == "schedule_tomorrow":
        USER_STATES.pop(sid, None)
        data = await schedule_model.get_daily_schedule(user.group_name, date.today() + timedelta(days=1))
        lessons = "\n".join(data["lessons"]) or "Пар немає."
        reply = f"📅 Розклад на завтра ({data['date']}) для {user.group_name}:\n{lessons}"

    elif action == "schedule_week":
        USER_STATES.pop(sid, None)
        days = await schedule_model.get_weekly_schedule(user.group_name)
        reply = "\n\n".join(
            f"📅 {d['date']}:\n" + ("\n".join(d["lessons"]) or "Пар немає")
            for d in days
        )

    elif action == "schedule_search":
        USER_STATES[sid] = "awaiting_schedule_search"
        reply = "🔍 Введіть назву групи (напр. ІПЗ-31) або прізвище викладача:"

    elif action == "menu_favorites":
        USER_STATES.pop(sid, None)
        favs = user_model.get_favorites(user)
        reply = (
            "⭐ Ваші вибрані розклади:\n" + "\n".join(f"• {f.target_name}" for f in favs)
            if favs
            else "⭐ Список вибраного порожній. Скористайтеся пошуком розкладу."
        )

    elif action == "menu_notifications":
        enabled = user_model.toggle_notifications(user)
        reply = f"🔔 Сповіщення: {'Увімкнено ✅' if enabled else 'Вимкнено ❌'}"

    # --- Обробка текстового вводу ---
    elif USER_STATES.get(sid) == "awaiting_schedule_search" and text:
        USER_STATES.pop(sid, None)
        data = await schedule_model.get_daily_schedule(text, date.today())
        lessons = "\n".join(data["lessons"]) or "Пар немає."
        user_model.add_favorite_schedule(user, target_name=text)
        reply = f"📅 Розклад для {text} ({data['date']}):\n{lessons}\n\n⭐ Додано у Вибране."

    elif text:
        result = await ai_model.process_query(user, text)
        reply = result["answer"]
        sources = result["sources"]

    else:
        reply = "Оберіть дію в меню або напишіть запитання."

    response = serialize_user_state(db, user)
    response["reply"] = reply
    response["sources"] = sources
    return response


# =====================================================================
# 2. ЕНДПОІНТИ АДМІН-ПАНЕЛІ
# =====================================================================

@app.post("/api/admin/notifications")
def create_notification(payload: NotificationRequest, db: Session = Depends(get_db)):
    notif = NotificationModel(db).create_notification(
        message_text=payload.message_text,
        category=payload.category,
        target_group=payload.target_group,
    )
    return {"status": "created", "notification_id": notif.id}


@app.put("/api/admin/schedule-config")
def update_schedule_config(payload: ScheduleConfigRequest, db: Session = Depends(get_db)):
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