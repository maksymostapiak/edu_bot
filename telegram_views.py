# telegram_views.py — шар View для формування inline-клавіатур Telegram

def get_main_menu_keyboard() -> dict:
    """Головне меню чат-бота (2x2 inline-кнопки)"""
    return {
        "inline_keyboard": [
            [
                {"text": "📅 Розклад", "callback_data": "menu_schedule"},
                {"text": "🤖 ШІ-консультація", "callback_data": "menu_ai"},
            ],
            [
                {"text": "⭐ Вибране", "callback_data": "menu_favorites"},
                {"text": "🔔 Сповіщення", "callback_data": "menu_notifications"},
            ],
        ]
    }


def get_schedule_keyboard() -> dict:
    """Підменю для роботи з розкладом"""
    return {
        "inline_keyboard": [
            [
                {"text": "Сьогодні", "callback_data": "schedule_today"},
                {"text": "Завтра", "callback_data": "schedule_tomorrow"},
                {"text": "Тиждень", "callback_data": "schedule_week"},
            ],
            [
                {"text": "🔍 Пошук за групою / викладачем", "callback_data": "schedule_search"},
            ],
            [
                {"text": "⬅️ Назад у головне меню", "callback_data": "back_to_main"},
            ],
        ]
    }