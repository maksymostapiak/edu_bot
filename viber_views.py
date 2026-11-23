def get_main_menu_keyboard() -> dict:
    """Головне меню чат-бота (4 кнопки у 2 ряди)"""
    return {
        "Type": "keyboard",
        "DefaultHeight": False,
        "BgColor": "#1E293B",
        "Buttons": [
            {
                "Columns": 3,
                "Rows": 1,
                "BgColor": "#7C3AED",
                "ActionType": "reply",
                "ActionBody": "menu_schedule",
                "Text": "<font color='#FFFFFF'><b>📅 Розклад</b></font>",
                "TextSize": "regular"
            },
            {
                "Columns": 3,
                "Rows": 1,
                "BgColor": "#0284C7",
                "ActionType": "reply",
                "ActionBody": "menu_ai",
                "Text": "<font color='#FFFFFF'><b>🤖 ШІ-консультація</b></font>",
                "TextSize": "regular"
            },
            {
                "Columns": 3,
                "Rows": 1,
                "BgColor": "#059669",
                "ActionType": "reply",
                "ActionBody": "menu_favorites",
                "Text": "<font color='#FFFFFF'><b>⭐ Вибране</b></font>",
                "TextSize": "regular"
            },
            {
                "Columns": 3,
                "Rows": 1,
                "BgColor": "#475569",
                "ActionType": "reply",
                "ActionBody": "menu_notifications",
                "Text": "<font color='#FFFFFF'><b>🔔 Сповіщення</b></font>",
                "TextSize": "regular"
            }
        ]
    }


def get_schedule_keyboard() -> dict:
    """Підменю для роботи з розкладом"""
    return {
        "Type": "keyboard",
        "DefaultHeight": False,
        "BgColor": "#1E293B",
        "Buttons": [
            {
                "Columns": 2,
                "Rows": 1,
                "BgColor": "#334155",
                "ActionType": "reply",
                "ActionBody": "schedule_today",
                "Text": "<font color='#FFFFFF'>Сьогодні</font>"
            },
            {
                "Columns": 2,
                "Rows": 1,
                "BgColor": "#334155",
                "ActionType": "reply",
                "ActionBody": "schedule_tomorrow",
                "Text": "<font color='#FFFFFF'>Завтра</font>"
            },
            {
                "Columns": 2,
                "Rows": 1,
                "BgColor": "#334155",
                "ActionType": "reply",
                "ActionBody": "schedule_week",
                "Text": "<font color='#FFFFFF'>Тиждень</font>"
            },
            {
                "Columns": 6,
                "Rows": 1,
                "BgColor": "#475569",
                "ActionType": "reply",
                "ActionBody": "schedule_search",
                "Text": "<font color='#FFFFFF'>🔍 Пошук за групою або викладачем</font>"
            },
            {
                "Columns": 6,
                "Rows": 1,
                "BgColor": "#0F172A",
                "ActionType": "reply",
                "ActionBody": "back_to_main",
                "Text": "<font color='#94A3B8'>⬅️ Назад у головне меню</font>"
            }
        ]
    }