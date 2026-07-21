from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

import jpholiday


JST = timezone(timedelta(hours=9), name="JST")
WINDOW_DAYS = 15
MONITOR_START = time(8, 0)
MONITOR_END = time(13, 0)
DATA_PATH = Path("data/availability.json")

AVAILABLE_KEYWORDS = ("予約可", "空き", "空", "○")
UNAVAILABLE_KEYWORDS = ("予約済み", "予約不可", "要問合せ", "満", "×")
TIME_RANGE_PATTERN = re.compile(
    r"(?P<start>\d{1,2}[:：]\d{2})\s*[〜~\-－–—]\s*"
    r"(?P<end>\d{1,2}[:：]\d{2})"
)


@dataclass(frozen=True)
class TargetDay:
    date: date
    day_type: str
    holiday_name: str | None


@dataclass(frozen=True)
class Facility:
    id: str
    name: str
    url_template: str
    selector_env: str
    scraper: Callable[["PageClient", "Facility", TargetDay], dict[str, Any]]


class PageClient(Protocol):
    def extract_texts(self, url: str, selector: str) -> list[str]: ...


class PlaywrightClient:
    """Small browser boundary so scraping can be replaced in unit tests."""

    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Any = None

    def __enter__(self) -> "PlaywrightClient":
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        return self

    def __exit__(self, *_: object) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()

    def extract_texts(self, url: str, selector: str) -> list[str]:
        if self._browser is None:
            raise RuntimeError("PlaywrightClient must be used as a context manager")

        context = self._browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            return page.locator(selector).all_inner_texts()
        finally:
            context.close()


def generate_target_days(
    start: date | None = None,
    days: int = WINDOW_DAYS,
) -> list[TargetDay]:
    """Return weekends and Japanese holidays within an inclusive 15-day window."""
    if days < 1:
        raise ValueError("days must be at least 1")

    first_day = start or datetime.now(JST).date()
    targets: list[TargetDay] = []
    for offset in range(days):
        current = first_day + timedelta(days=offset)
        holiday = jpholiday.is_holiday_name(current)
        if holiday:
            targets.append(TargetDay(current, "holiday", holiday))
        elif current.weekday() >= 5:
            targets.append(TargetDay(current, "weekend", None))
    return targets


def parse_clock(value: str) -> time:
    normalized = value.replace("：", ":")
    hour, minute = (int(part) for part in normalized.split(":"))
    return time(hour, minute)


def overlaps_monitor_window(start: str, end: str) -> bool:
    slot_start = parse_clock(start)
    slot_end = parse_clock(end)
    return max(slot_start, MONITOR_START) < min(slot_end, MONITOR_END)


def parse_slot_texts(texts: Iterable[str], booking_url: str) -> list[dict[str, str]]:
    """Parse candidate text after a facility-specific DOM selector has narrowed it."""
    slots: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for raw_text in texts:
        text_value = re.sub(r"\s+", " ", raw_text or "").strip()
        if not text_value or any(word in text_value for word in UNAVAILABLE_KEYWORDS):
            continue
        if not any(word in text_value for word in AVAILABLE_KEYWORDS):
            continue

        for match in TIME_RANGE_PATTERN.finditer(text_value):
            start = match.group("start").replace("：", ":").zfill(5)
            end = match.group("end").replace("：", ":").zfill(5)
            if not overlaps_monitor_window(start, end):
                continue

            key = (start, end, text_value)
            if key in seen:
                continue
            seen.add(key)
            slots.append(
                {
                    "start": start,
                    "end": end,
                    "court": text_value[:200],
                    "status": "available",
                    "booking_url": booking_url,
                }
            )

    return slots


def _pending_result(target: TargetDay, message: str) -> dict[str, Any]:
    return {
        "date": target.date.isoformat(),
        "day_type": target.day_type,
        "holiday_name": target.holiday_name,
        "status": "selector_pending",
        "message": message,
        "slots": [],
    }


def _scrape_with_selector(
    client: PageClient,
    facility: Facility,
    target: TargetDay,
) -> dict[str, Any]:
    selector = os.getenv(facility.selector_env, "").strip()
    if not selector:
        return _pending_result(
            target,
            f"{facility.selector_env} is not configured",
        )

    url = facility.url_template.format(date=target.date.isoformat())
    texts = client.extract_texts(url, selector)
    return {
        "date": target.date.isoformat(),
        "day_type": target.day_type,
        "holiday_name": target.holiday_name,
        "status": "ok",
        "message": None,
        "slots": parse_slot_texts(texts, url),
    }


def scrape_kamoike(
    client: PageClient,
    facility: Facility,
    target: TargetDay,
) -> dict[str, Any]:
    """鴨池県営向け。実DOMのセレクタ調整はこの関数境界内で行う。"""
    return _scrape_with_selector(client, facility, target)


def scrape_sumizei(
    client: PageClient,
    facility: Facility,
    target: TargetDay,
) -> dict[str, Any]:
    """SuMIzei向け。実DOMのセレクタ調整はこの関数境界内で行う。"""
    if not facility.url_template:
        return _pending_result(target, "SUMIZEI_URL_TEMPLATE is not configured")
    return _scrape_with_selector(client, facility, target)


def configured_facilities() -> tuple[Facility, ...]:
    return (
        Facility(
            id="kamoike",
            name="鴨池県営テニスコート",
            url_template=(
                "https://v2.spm-cloud.com/user/kamoike-undo/reserves/daily"
                "?date={date}&category_id=483&area_id=289"
            ),
            selector_env="KAMOIKE_SLOT_SELECTOR",
            scraper=scrape_kamoike,
        ),
        Facility(
            id="sumizei",
            name="SuMIzei",
            url_template=os.getenv("SUMIZEI_URL_TEMPLATE", "").strip(),
            selector_env="SUMIZEI_SLOT_SELECTOR",
            scraper=scrape_sumizei,
        ),
    )


def empty_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": None,
        "window": {
            "days": WINDOW_DAYS,
            "start": MONITOR_START.strftime("%H:%M"),
            "end": MONITOR_END.strftime("%H:%M"),
            "timezone": "Asia/Tokyo",
        },
        "facilities": [],
    }


def build_document(
    targets: list[TargetDay],
    facilities: Iterable[Facility] | None = None,
    client_factory: Callable[[], PlaywrightClient] = PlaywrightClient,
) -> dict[str, Any]:
    selected_facilities = tuple(facilities or configured_facilities())
    needs_browser = any(
        facility.url_template and os.getenv(facility.selector_env, "").strip()
        for facility in selected_facilities
    )

    client_context: Any
    if needs_browser:
        client_context = client_factory()
    else:
        client_context = _NoopClientContext()

    facility_results: list[dict[str, Any]] = []
    with client_context as client:
        for facility in selected_facilities:
            dates: list[dict[str, Any]] = []
            for target in targets:
                try:
                    dates.append(facility.scraper(client, facility, target))
                except Exception as exc:
                    dates.append(
                        {
                            "date": target.date.isoformat(),
                            "day_type": target.day_type,
                            "holiday_name": target.holiday_name,
                            "status": "error",
                            "message": str(exc),
                            "slots": [],
                        }
                    )
            facility_results.append(
                {
                    "id": facility.id,
                    "name": facility.name,
                    "dates": dates,
                }
            )

    document = empty_document()
    document["generated_at"] = datetime.now(JST).isoformat(timespec="seconds")
    document["facilities"] = facility_results
    return document


class _NoopClientContext:
    def __enter__(self) -> "_NoopClientContext":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def extract_texts(self, url: str, selector: str) -> list[str]:
        raise RuntimeError(f"Browser was not configured for {url} ({selector})")


def available_slot_keys(document: dict[str, Any]) -> dict[tuple[str, ...], dict[str, str]]:
    keys: dict[tuple[str, ...], dict[str, str]] = {}
    for facility in document.get("facilities", []):
        for date_entry in facility.get("dates", []):
            for slot in date_entry.get("slots", []):
                if slot.get("status") != "available":
                    continue
                key = (
                    str(facility.get("id", "")),
                    str(date_entry.get("date", "")),
                    str(slot.get("start", "")),
                    str(slot.get("end", "")),
                    str(slot.get("court", "")),
                )
                keys[key] = {
                    "facility_id": key[0],
                    "facility_name": str(facility.get("name", key[0])),
                    "date": key[1],
                    "start": key[2],
                    "end": key[3],
                    "court": key[4],
                    "booking_url": str(slot.get("booking_url", "")),
                }
    return keys


def detect_new_availability(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> list[dict[str, str]]:
    old_keys = available_slot_keys(previous)
    current_keys = available_slot_keys(current)
    return [current_keys[key] for key in sorted(current_keys.keys() - old_keys.keys())]


def build_line_message(changes: list[dict[str, str]]) -> str:
    lines = ["【テニスコート空き情報】", "新しい空き候補が見つかりました。"]
    for change in changes[:10]:
        lines.append(
            f"・{change['facility_name']} {change['date']} "
            f"{change['start']}〜{change['end']} {change['court']}"
        )
    if len(changes) > 10:
        lines.append(f"ほか {len(changes) - 10} 件")
    return "\n".join(lines)[:4900]


def send_line_notification(
    changes: list[dict[str, str]],
    token: str | None = None,
    user_id: str | None = None,
) -> bool:
    if not changes:
        return False

    line_token = token or os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    line_user_id = user_id or os.getenv("LINE_USER_ID")
    if not line_token or not line_user_id:
        print("LINE secrets are missing; notification skipped.")
        return False

    payload = {
        "to": line_user_id,
        "messages": [{"type": "text", "text": build_line_message(changes)}],
    }
    request = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {line_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        print("LINE status:", response.status)
    return True


def load_document(path: Path = DATA_PATH) -> dict[str, Any]:
    if not path.exists():
        return empty_document()
    return json.loads(path.read_text(encoding="utf-8"))


def write_document(document: dict[str, Any], path: Path = DATA_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def comparable_document(document: dict[str, Any]) -> dict[str, Any]:
    comparable = dict(document)
    comparable.pop("generated_at", None)
    return comparable


def main() -> int:
    previous = load_document()
    targets = generate_target_days()
    current = build_document(targets)
    changes = detect_new_availability(previous, current)
    if comparable_document(previous) != comparable_document(current):
        write_document(current)
        result = "updated"
    else:
        result = "unchanged"
    print(
        f"target_days={len(targets)} new_slots={len(changes)} data={result}"
    )
    send_line_notification(changes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
