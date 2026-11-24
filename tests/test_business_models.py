import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, KnowledgeChunk, Notification, ScheduleCache, User
from business_models import (
    AiConsultationModel,
    NotificationModel,
    ScheduleModel,
    UserModel,
)


@pytest.fixture
def db_session():
    """Ізольована in-memory БД для кожного тесту."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def reset_schedule_class_cache():
    """Скидаємо класовий кеш списку груп між тестами."""
    ScheduleModel._groups_cache = []
    ScheduleModel._groups_fetched_at = None
    yield
    ScheduleModel._groups_cache = []
    ScheduleModel._groups_fetched_at = None


# =====================================================================
# 1. ТЕСТИ UserModel
# =====================================================================

def test_user_get_or_create_defaults(db_session):
    model = UserModel(db_session)
    user = model.get_or_create("sess-1", full_name="Максим")

    assert user.id is not None
    assert user.session_id == "sess-1"
    assert user.full_name == "Максим"
    assert user.group_name == "ІПЗ-31"
    assert user.notifications_enabled is True

    # Повторний виклик повертає того самого користувача
    same_user = model.get_or_create("sess-1")
    assert same_user.id == user.id


def test_user_toggle_notifications_and_favorites(db_session):
    model = UserModel(db_session)
    user = model.get_or_create("sess-2")

    assert model.toggle_notifications(user) is False
    assert model.toggle_notifications(user) is True

    fav1 = model.add_favorite_schedule(user, "ІПЗ-31")
    fav2 = model.add_favorite_schedule(user, "ІПЗ-31")  # дублікат не створюється
    model.add_favorite_schedule(user, "КН-21")

    assert fav1.id == fav2.id
    assert [f.target_name for f in model.get_favorites(user)] == ["ІПЗ-31", "КН-21"]


# =====================================================================
# 2. ТЕСТИ ScheduleModel
# =====================================================================

def test_build_group_url_negative_and_positive_id():
    assert (
        ScheduleModel._build_group_url(1002, -6326)
        == "https://asu.pnu.edu.ua/static/groups/1002/0-6326/index.js"
    )
    assert (
        ScheduleModel._build_group_url(1002, 123)
        == "https://asu.pnu.edu.ua/static/groups/1002/000123/index.js"
    )


def test_js_array_to_json_with_unquoted_keys_and_colons():
    js_code = """
    const PNUschedule = [
      {li: 1, d: "2026-11-24", ls: "10:35", le: "11:55", l: "Фізика: лекція", lt: "Лк", g: "ІПЗ-31", t: "Іванов І.І.", vr: "403"},
    ];
    """
    parsed = ScheduleModel._js_array_to_json(js_code, "PNUschedule")
    assert len(parsed) == 1
    assert parsed[0]["ls"] == "10:35"
    assert parsed[0]["l"] == "Фізика: лекція"


def test_get_daily_schedule_uses_groups_list_and_caches(db_session):
    groups_js = """
    const PNUgroups = [
      {f: 1002, i: -6326, c: 3, l: "ІПЗ-31"}
    ];
    """
    schedule_js = """
    const PNUschedule = [
      {li: 1, d: "2026-11-24", ls: "08:30", le: "09:50", l: "Бази даних", lt: "ПрС", g: "ІПЗ-31", t: "Петренко П.П.", vr: "201"}
    ];
    """

    requested_urls = []

    class FakeResponse:
        def __init__(self, text, status_code=200):
            self.text = text
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            requested_urls.append(url)
            if "groups-list.js" in url:
                return FakeResponse(groups_js)
            return FakeResponse(schedule_js)

    model = ScheduleModel(db_session)
    with patch("business_models.httpx.AsyncClient", FakeAsyncClient):
        res1 = asyncio.run(model.get_daily_schedule("ІПЗ-31", date(2026, 11, 24)))
        assert len(res1["lessons"]) == 1
        assert "Бази даних" in res1["lessons"][0]
        assert "https://asu.pnu.edu.ua/static/groups/1002/0-6326/index.js" in requested_urls

        # Другий запит має взяти дані з кешу БД без повторного запиту до розкладу
        calls_before = len(requested_urls)
        res2 = asyncio.run(model.get_daily_schedule("ІПЗ-31", date(2026, 11, 24)))
        assert res2["lessons"] == res1["lessons"]
        assert len(requested_urls) == calls_before


# =====================================================================
# 3. ТЕСТИ AiConsultationModel (RAG + документи)
# =====================================================================

def test_upload_list_and_delete_document(db_session):
    ai = AiConsultationModel(db_session)
    ai.client = None  # без зовнішнього виклику Gemini — повертає вектор нулів [0.0] * 1536

    content = ("Правила складання сесії в університеті. " * 20).encode("utf-8")
    chunks_count = asyncio.run(
        ai.upload_document("rules.txt", content, source_url="https://pnu.edu.ua/rules", chunk_size=120)
    )

    assert chunks_count > 1
    docs = ai.list_documents()
    assert len(docs) == 1
    assert docs[0]["document_name"] == "rules.txt"
    assert docs[0]["chunks_count"] == chunks_count
    assert docs[0]["source_url"] == "https://pnu.edu.ua/rules"

    deleted = ai.delete_document("rules.txt")
    assert deleted == chunks_count
    assert ai.list_documents() == []


def test_upload_unsupported_format_raises(db_session):
    ai = AiConsultationModel(db_session)
    with pytest.raises(ValueError):
        asyncio.run(ai.upload_document("archive.zip", b"PK\x03\x04"))


def test_generate_llm_response_prefers_groq(db_session):
    ai = AiConsultationModel(db_session)
    ai.groq_api_key = "test-groq-key"
    ai._generate_via_groq = AsyncMock(return_value="Відповідь від Groq")

    reply = asyncio.run(ai.generate_llm_response("Коли сесія?", "Контекст", []))
    assert reply == "Відповідь від Groq"
    ai._generate_via_groq.assert_awaited_once()


# =====================================================================
# 4. ТЕСТИ NotificationModel
# =====================================================================

def test_notifications_filtering_and_toggle(db_session):
    user_model = UserModel(db_session)
    notif_model = NotificationModel(db_session)

    user = user_model.get_or_create("sess-notif")
    user.group_name = "ІПЗ-31"
    db_session.commit()

    notif_model.create_notification("Загальне оголошення", "general", target_group=None)
    notif_model.create_notification("Зміна пари ІПЗ-31", "schedule_changes", target_group="ІПЗ-31")
    notif_model.create_notification("Новина для іншої групи", "general", target_group="КН-21")

    visible = notif_model.get_user_notifications(user)
    texts = [n.message_text for n in visible]
    assert "Загальне оголошення" in texts
    assert "Зміна пари ІПЗ-31" in texts
    assert "Новина для іншої групи" not in texts

    user_model.toggle_notifications(user)
    assert notif_model.get_user_notifications(user) == []