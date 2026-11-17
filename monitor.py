"""Monitor Altegio tennis-court availability and notify a Telegram chat.

Designed for one-shot scheduled execution (for example, GitHub Actions cron).
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
BOOKING_FORM_ID = os.environ.get("BOOKING_FORM_ID", "551098").strip()
COMPANY_ID = os.environ.get("COMPANY_ID", "521176").strip()
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "7"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "seen_slots.json"))

BASE_API = "https://api.alteg.io/api/v1"
PARTNER_TOKEN = f"app-id={BOOKING_FORM_ID}"
HEADERS = {
    "Accept": "application/vnd.api.v2+json",
    "Authorization": f"Bearer {PARTNER_TOKEN}",
    "Content-Type": "application/json",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("tennis-court-monitor")
session = requests.Session()


def validate_config() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if not BOOKING_FORM_ID:
        missing.append("BOOKING_FORM_ID")
    if not COMPANY_ID:
        missing.append("COMPANY_ID")
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))
    if DAYS_AHEAD < 0:
        raise RuntimeError("DAYS_AHEAD must be zero or greater")


def api_get(url: str) -> dict[str, Any]:
    response = session.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected API response from {url}")
    return payload


def get_staff_list() -> list[dict[str, Any]]:
    data = api_get(f"{BASE_API}/book_staff/{COMPANY_ID}")
    return data.get("data", [])


def get_services() -> list[dict[str, Any]]:
    data = api_get(f"{BASE_API}/book_services/{COMPANY_ID}")
    services: list[dict[str, Any]] = []
    for category in data.get("data", []):
        services.extend(category.get("services", []))
    return services


def get_available_dates(staff_id: int, service_id: int) -> list[Any]:
    date_from = datetime.now().strftime("%Y-%m-%d")
    date_to = (datetime.now() + timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%d")
    url = (
        f"{BASE_API}/book_dates/{COMPANY_ID}"
        f"?staff_id={staff_id}&service_id={service_id}"
        f"&date_from={date_from}&date_to={date_to}"
    )
    return api_get(url).get("data", [])


def get_time_slots(staff_id: int, service_id: int, date: str) -> list[dict[str, Any]]:
    url = f"{BASE_API}/book_times/{COMPANY_ID}/{staff_id}/{date}?service_id={service_id}"
    return api_get(url).get("data", [])


def send_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        response = session.post(url, json=payload, timeout=15)
        response.raise_for_status()
        result = response.json()
        if not result.get("ok", False):
            raise RuntimeError(f"Telegram API returned: {result}")
        return True
    except Exception:
        log.exception("Telegram notification failed")
        return False


def load_previous_slots() -> set[str]:
    if not STATE_FILE.exists():
        log.info("No previous state found; this run will establish the initial baseline and notify currently available slots.")
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("slots", []))
    except Exception:
        log.exception("Could not read saved state; refusing to overwrite it")
        raise


def save_current_slots(slots: set[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    temporary_file.write_text(
        json.dumps({"updated_at": datetime.utcnow().isoformat() + "Z", "slots": sorted(slots)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_file.replace(STATE_FILE)


def slot_identity(staff_id: int, service_id: int, date: str, slot: dict[str, Any]) -> str:
    slot_time = str(slot.get("time", "")).strip()
    return f"{staff_id}|{service_id}|{date}|{slot_time}"


def format_slot_message(staff_name: str, service_name: str, date: str, slots: list[dict[str, Any]]) -> str:
    booking_url = f"https://b{BOOKING_FORM_ID}.alteg.io/company/{COMPANY_ID}/personal/select-master?o="
    times = ", ".join(str(slot.get("time", "?")) for slot in slots[:10])
    if len(slots) > 10:
        times += f" and {len(slots) - 10} more..."
    return (
        "🎾 <b>Свободные слоты на корт!</b>\n\n"
        f"📍 <b>Корт:</b> {staff_name}\n"
        f"🏸 <b>Услуга:</b> {service_name}\n"
        f"📅 <b>Дата:</b> {date}\n"
        f"⏰ <b>Время:</b> {times}\n\n"
        f"👉 <a href='{booking_url}'>Забронировать</a>"
    )


def check_slots() -> None:
    log.info("Checking available tennis-court slots...")
    previous_slots = load_previous_slots()
    current_slots: set[str] = set()
    notifications_sent = 0

    staff_list = get_staff_list()
    if not staff_list:
        raise RuntimeError("Altegio returned an empty staff/court list; state was not updated")

    services = get_services()
    if not services:
        log.warning("No services returned; falling back to service_id=0")
        services = [{"id": 0, "title": "Аренда корта"}]

    for staff in staff_list:
        staff_id = staff.get("id")
        if staff_id is None:
            continue
        staff_name = staff.get("name", f"Корт {staff_id}")

        for service in services:
            service_id = service.get("id", 0)
            service_name = service.get("title", "Аренда")
            dates = get_available_dates(staff_id, service_id)

            for date_info in dates:
                date = date_info if isinstance(date_info, str) else date_info.get("date", "")
                if not date:
                    continue
                slots = get_time_slots(staff_id, service_id, date)
                if not slots:
                    continue

                new_slots = []
                for slot in slots:
                    if not isinstance(slot, dict) or not slot.get("time"):
                        continue
                    identity = slot_identity(staff_id, service_id, date, slot)
                    current_slots.add(identity)
                    if identity not in previous_slots:
                        new_slots.append(slot)

                if new_slots:
                    message = format_slot_message(staff_name, service_name, date, new_slots)
                    if not send_telegram(message):
                        raise RuntimeError("Could not send one or more Telegram notifications; state was not updated")
                    notifications_sent += 1
                    log.info("New slots: %s / %s — %s", staff_name, date, len(new_slots))
                    time.sleep(0.5)

    save_current_slots(current_slots)
    log.info("Check complete. Notifications sent: %s; currently available slots: %s", notifications_sent, len(current_slots))


def main() -> None:
    validate_config()
    log.info("Monitor started | company=%s | form=%s | horizon=%s days", COMPANY_ID, BOOKING_FORM_ID, DAYS_AHEAD)
    check_slots()


if __name__ == "__main__":
    main()
