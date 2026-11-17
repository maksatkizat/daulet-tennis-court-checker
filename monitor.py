"""
🎾 Мониторинг свободных кортов на Altegio
Отправляет уведомление в Telegram когда появляются свободные слоты.

Установка зависимостей:
    pip install requests python-telegram-bot schedule

Запуск:
    python monitor.py
"""

import os
import requests
import json
import sys
import time
import schedule
import logging
from datetime import datetime, timedelta
from typing import Optional

# ═══════════════════════════════════════════════════════
#  ⚙️  НАСТРОЙКИ — ЗАПОЛНИ ЭТО ПЕРЕД ЗАПУСКОМ
# ═══════════════════════════════════════════════════════

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "ТВОЙ_ТОКЕН_БОТА")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "ТВОЙ_CHAT_ID")

# Из URL: https://b551098.alteg.io/company/521176/...
BOOKING_FORM_ID = os.environ.get("BOOKING_FORM_ID", "551098")
COMPANY_ID      = os.environ.get("COMPANY_ID", "521176")

# Сколько дней вперёд проверять
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "7"))

# ═══════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        # logging.FileHandler("monitor.log", encoding="utf-8"),  # отключено для Render
    ]
)
log = logging.getLogger(__name__)

# Храним уже отправленные слоты чтобы не спамить повторно
seen_slots: set = set()


# ───────────────────────────────────────────────────────
# Altegio API
# ───────────────────────────────────────────────────────

BASE_API = "https://api.alteg.io/api/v1"
PARTNER_TOKEN = f"app-id={BOOKING_FORM_ID}"   # публичный токен из ID формы

HEADERS = {
    "Accept":        "application/vnd.api.v2+json",
    "Authorization": f"Bearer {PARTNER_TOKEN}",
    "Content-Type":  "application/json",
}


def get_staff_list() -> list:
    """Получить список сотрудников / кортов для данной компании."""
    url = f"{BASE_API}/book_staff/{COMPANY_ID}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        return data.get("data", [])
    except Exception as e:
        log.error(f"Ошибка получения списка кортов: {e}")
        return []


def get_services() -> list:
    """Получить список услуг компании."""
    url = f"{BASE_API}/book_services/{COMPANY_ID}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        # Вернуть все услуги из всех категорий
        services = []
        for cat in data.get("data", []):
            services.extend(cat.get("services", []))
        return services
    except Exception as e:
        log.error(f"Ошибка получения услуг: {e}")
        return []


def get_available_dates(staff_id: int, service_id: int) -> list:
    """Получить даты с доступными слотами."""
    date_from = datetime.now().strftime("%Y-%m-%d")
    date_to   = (datetime.now() + timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%d")
    url = (
        f"{BASE_API}/book_dates/{COMPANY_ID}"
        f"?staff_id={staff_id}&service_id={service_id}"
        f"&date_from={date_from}&date_to={date_to}"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        log.error(f"Ошибка получения дат (staff={staff_id}): {e}")
        return []


def get_time_slots(staff_id: int, service_id: int, date: str) -> list:
    """Получить свободные временные слоты на конкретную дату."""
    url = (
        f"{BASE_API}/book_times/{COMPANY_ID}/{staff_id}/{date}"
        f"?service_id={service_id}"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        log.error(f"Ошибка получения слотов ({date}, staff={staff_id}): {e}")
        return []


# ───────────────────────────────────────────────────────
# Telegram
# ───────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    """Отправить сообщение в Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Ошибка отправки в Telegram: {e}")
        return False


def format_slot_message(staff_name: str, service_name: str,
                         date: str, slots: list) -> str:
    """Сформировать текст уведомления."""
    booking_url = (
        f"https://b{BOOKING_FORM_ID}.alteg.io"
        f"/company/{COMPANY_ID}/personal/select-master?o="
    )
    times = ", ".join(s["time"] for s in slots[:10])
    if len(slots) > 10:
        times += f" и ещё {len(slots) - 10}..."

    return (
        f"🎾 <b>Свободные слоты на корт!</b>\n\n"
        f"📍 <b>Корт:</b> {staff_name}\n"
        f"🏸 <b>Услуга:</b> {service_name}\n"
        f"📅 <b>Дата:</b> {date}\n"
        f"⏰ <b>Время:</b> {times}\n\n"
        f"👉 <a href='{booking_url}'>Забронировать</a>"
    )


# ───────────────────────────────────────────────────────
# Основная логика проверки
# ───────────────────────────────────────────────────────

def check_slots():
    """Главная функция: проверить все корты и услуги."""
    log.info("⏳ Проверяю доступные слоты...")

    staff_list = get_staff_list()
    if not staff_list:
        log.warning("Список кортов пуст или API недоступен")
        return

    services = get_services()
    if not services:
        # Попробуем без фильтра по услуге (service_id=0 — все услуги)
        services = [{"id": 0, "title": "Аренда корта"}]

    new_slots_found = 0

    for staff in staff_list:
        staff_id   = staff.get("id")
        staff_name = staff.get("name", f"Корт {staff_id}")

        for service in services:
            service_id   = service.get("id", 0)
            service_name = service.get("title", "Аренда")

            dates = get_available_dates(staff_id, service_id)

            for date_info in dates:
                date = date_info if isinstance(date_info, str) else date_info.get("date", "")
                if not date:
                    continue

                slots = get_time_slots(staff_id, service_id, date)
                if not slots:
                    continue

                # Создаём уникальный ключ для этого набора слотов
                slot_key = f"{staff_id}_{service_id}_{date}_{len(slots)}"

                if slot_key not in seen_slots:
                    seen_slots.add(slot_key)
                    msg = format_slot_message(staff_name, service_name, date, slots)
                    log.info(f"✅ Новые слоты: {staff_name} / {date} — {len(slots)} шт.")
                    send_telegram(msg)
                    new_slots_found += 1
                    time.sleep(0.5)  # небольшая пауза между сообщениями

    if new_slots_found == 0:
        log.info("ℹ️  Новых слотов не найдено")
    else:
        log.info(f"📩 Отправлено уведомлений: {new_slots_found}")


def main():
    log.info("🚀 Мониторинг кортов запущен")
    log.info(f"   Компания ID : {COMPANY_ID}")
    log.info(f"   Форма ID    : {BOOKING_FORM_ID}")
    log.info(f"   Горизонт    : {DAYS_AHEAD} дней")

    # Однократная проверка (для Render Cron Job)
    check_slots()


if __name__ == "__main__":
    main()
