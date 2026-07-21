from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

import jpholiday
from bs4 import BeautifulSoup, Tag


JST = timezone(timedelta(hours=9), name="JST")
WINDOW_DAYS = 15
MONITOR_START = time(8, 0)
MONITOR_END = time(13, 0)
MINIMUM_DURATION_MINUTES = 60
DATA_PATH = Path("data/availability.json")
SNAPSHOT_ROOT = Path("snapshots")

KAMOIKE_FACILITY_ID = "kamoike-prefectural"
KAMOIKE_FACILITY_NAME = "鴨池県営テニスコート"
KAMOIKE_URL_TEMPLATE = (
    "https://v2.spm-cloud.com/user/kamoike-undo/reserves/daily"
    "?date={date}&category_id=483&area_id=289"
)

WIDTH_PATTERN = re.compile(r"width\s*:\s*([\d.]+)%", re.IGNORECASE)
STATE_CLASS_PATTERN = re.compile(r"rsv--result--(yes|no|out)")
ACCESS_DENIED_MARKERS = (
    "access denied",
    "forbidden",
    "too many requests",
    "アクセスが拒否",
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
    requires_browser: bool = False


@dataclass(frozen=True)
class PageCapture:
    html: str
    checked_at: str
    response_status: int | None
    error_type: str | None = None
    error_message: str | None = None


class ScrapeStructureError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class PageClient(Protocol):
    def capture_page(
        self,
        url: str,
        snapshot_directory: Path,
        snapshot_name: str,
    ) -> PageCapture: ...

    def extract_texts(self, url: str, selector: str) -> list[str]: ...


class PlaywrightClient:
    """Browser boundary shared by facility scrapers and replaceable in tests."""

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

    def capture_page(
        self,
        url: str,
        snapshot_directory: Path,
        snapshot_name: str,
    ) -> PageCapture:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        if self._browser is None:
            raise RuntimeError("PlaywrightClient must be used as a context manager")

        snapshot_directory.mkdir(parents=True, exist_ok=True)
        html_path = snapshot_directory / f"{snapshot_name}.html"
        image_path = snapshot_directory / f"{snapshot_name}.png"
        checked_at = datetime.now(JST).isoformat(timespec="seconds")
        response_status: int | None = None
        error_type: str | None = None
        error_message: str | None = None

        context = self._browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 1440, "height": 1000},
        )
        page = context.new_page()
        try:
            try:
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                response_status = response.status if response else None
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except PlaywrightTimeoutError:
                    pass
                try:
                    page.locator("#app").wait_for(state="attached", timeout=15_000)
                except PlaywrightTimeoutError:
                    pass
                try:
                    page.locator(".rsv__result[data-reserve]").wait_for(
                        state="attached",
                        timeout=20_000,
                    )
                except PlaywrightTimeoutError:
                    pass
            except PlaywrightTimeoutError as exc:
                error_type = "navigation_timeout"
                error_message = str(exc)
            except Exception as exc:
                error_type = "navigation_error"
                error_message = str(exc)

            html = ""
            for _ in range(3):
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=5_000)
                except PlaywrightTimeoutError:
                    pass
                try:
                    html = page.content()
                    break
                except Exception as exc:
                    error_message = (
                        f"{error_message}; content capture failed: {exc}"
                        if error_message
                        else f"content capture failed: {exc}"
                    )
                    page.wait_for_timeout(500)
            if not html:
                error_type = error_type or "navigation_error"
                html = (
                    "<!doctype html><html><body><h1>Capture failed</h1>"
                    f"<pre>{error_message or error_type}</pre></body></html>"
                )
            html_path.write_text(html, encoding="utf-8")
            for _ in range(3):
                try:
                    page.screenshot(path=str(image_path), full_page=True)
                    break
                except Exception as exc:
                    screenshot_error = f"screenshot failed: {exc}"
                    error_message = (
                        f"{error_message}; {screenshot_error}"
                        if error_message
                        else screenshot_error
                    )
                    page.wait_for_timeout(500)
            if not image_path.exists():
                error_type = error_type or "snapshot_error"
                error_message = error_message or "Screenshot could not be saved"

            if response_status in {401, 403, 429}:
                error_type = "access_denied"
                error_message = f"HTTP {response_status}"

            return PageCapture(
                html=html,
                checked_at=checked_at,
                response_status=response_status,
                error_type=error_type,
                error_message=error_message,
            )
        finally:
            context.close()

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


def clock_to_minutes(value: str | time) -> int:
    parsed = parse_clock(value) if isinstance(value, str) else value
    return parsed.hour * 60 + parsed.minute


def minutes_to_clock(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def overlaps_monitor_window(start: str, end: str) -> bool:
    slot_start = parse_clock(start)
    slot_end = parse_clock(end)
    return max(slot_start, MONITOR_START) < min(slot_end, MONITOR_END)


def make_slot_id(
    facility_id: str,
    target_date: str,
    court_name: str,
    start_time: str,
    end_time: str,
) -> str:
    source = "|".join(
        (facility_id, target_date, court_name, start_time, end_time)
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]


def make_availability_slot(
    facility_id: str,
    facility_name: str,
    target_date: str,
    court_name: str,
    start_minutes: int,
    end_minutes: int,
    reservation_url: str,
) -> dict[str, Any]:
    start_time = minutes_to_clock(start_minutes)
    end_time = minutes_to_clock(end_minutes)
    return {
        "facility_id": facility_id,
        "facility_name": facility_name,
        "date": target_date,
        "court_name": court_name,
        "start_time": start_time,
        "end_time": end_time,
        "duration_minutes": end_minutes - start_minutes,
        "status": "available",
        "reservation_url": reservation_url,
        "slot_id": make_slot_id(
            facility_id,
            target_date,
            court_name,
            start_time,
            end_time,
        ),
    }


def _style_width(element: Tag) -> float:
    match = WIDTH_PATTERN.search(element.get("style", ""))
    if not match:
        raise ScrapeStructureError(
            "unexpected_dom",
            "A reservation state cell has no percentage width",
        )
    return float(match.group(1))


def _is_hidden(element: Tag) -> bool:
    if element.has_attr("hidden") or element.get("aria-hidden") == "true":
        return True
    style = element.get("style", "").replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


def merge_consecutive_slots(slots: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only merged ranges; atomic ranges are not retained separately."""
    deduplicated = {slot["slot_id"]: dict(slot) for slot in slots}
    ordered = sorted(
        deduplicated.values(),
        key=lambda slot: (
            slot["date"],
            natural_sort_key(slot["court_name"]),
            slot["start_time"],
            slot["end_time"],
        ),
    )
    merged: list[dict[str, Any]] = []

    for slot in ordered:
        if (
            merged
            and merged[-1]["date"] == slot["date"]
            and merged[-1]["court_name"] == slot["court_name"]
            and merged[-1]["end_time"] == slot["start_time"]
        ):
            current = merged[-1]
            current["end_time"] = slot["end_time"]
            current["duration_minutes"] += slot["duration_minutes"]
            current["slot_id"] = make_slot_id(
                current["facility_id"],
                current["date"],
                current["court_name"],
                current["start_time"],
                current["end_time"],
            )
        else:
            merged.append(dict(slot))
    return merged


def natural_sort_key(value: str) -> tuple[Any, ...]:
    return tuple(
        int(part) if part.isdigit() else part
        for part in re.split(r"(\d+)", value)
    )


def parse_kamoike_html(
    html: str,
    target: TargetDay,
    reservation_url: str,
    checked_at: str,
) -> dict[str, Any]:
    """Parse Vue-rendered court rows observed on the live reservation page."""
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True).lower()
    if any(marker in page_text for marker in ACCESS_DENIED_MARKERS):
        raise ScrapeStructureError("access_denied", "Access denied page detected")

    roots = [
        root
        for root in soup.select(".rsv__result[data-reserve]")
        if isinstance(root, Tag) and not _is_hidden(root)
    ]
    if not roots:
        raise ScrapeStructureError(
            "no_schedule_table",
            "No visible .rsv__result[data-reserve] schedule was found",
        )

    raw_slots: list[dict[str, Any]] = []
    row_count = 0
    for root in roots:
        for field in root.select(":scope > section.rsv__field"):
            if not isinstance(field, Tag) or _is_hidden(field):
                continue
            court_element = field.select_one(
                "h3.rsv__result__item:not(.major--item--color) em"
            )
            if court_element is None:
                continue
            court_name = court_element.get_text(" ", strip=True)
            if not court_name:
                raise ScrapeStructureError(
                    "unexpected_dom",
                    "A court row has no court name",
                )

            time_elements = field.select(".rsv__result__time > li")
            state_elements = field.select(".rsv__result__situation > li")
            if len(time_elements) < 2 or not state_elements:
                raise ScrapeStructureError(
                    "unexpected_dom",
                    f"Missing time header or state cells for {court_name}",
                )

            time_labels = [
                element.get_text(" ", strip=True) for element in time_elements
            ]
            try:
                grid_start = clock_to_minutes(time_labels[0])
                grid_end = clock_to_minutes(time_labels[-1])
            except (ValueError, IndexError) as exc:
                raise ScrapeStructureError(
                    "unexpected_dom",
                    f"Invalid time header for {court_name}: {time_labels}",
                ) from exc
            if grid_end <= grid_start:
                raise ScrapeStructureError(
                    "unexpected_dom",
                    f"Invalid time range for {court_name}: {time_labels}",
                )

            active_cells: list[tuple[Tag, str, float]] = []
            for element in state_elements:
                if not isinstance(element, Tag):
                    continue
                state_match = STATE_CLASS_PATTERN.search(" ".join(element.get("class", [])))
                if state_match:
                    active_cells.append(
                        (element, state_match.group(1), _style_width(element))
                    )
            if not active_cells:
                raise ScrapeStructureError(
                    "unexpected_dom",
                    f"No classified reservation cells for {court_name}",
                )

            active_width = sum(width for _, _, width in active_cells)
            if active_width <= 0:
                raise ScrapeStructureError(
                    "unexpected_dom",
                    f"Invalid reservation cell widths for {court_name}",
                )

            row_count += 1
            elapsed_width = 0.0
            grid_minutes = grid_end - grid_start
            for element, state, width in active_cells:
                segment_start = grid_start + round(
                    elapsed_width / active_width * grid_minutes
                )
                elapsed_width += width
                segment_end = grid_start + round(
                    elapsed_width / active_width * grid_minutes
                )
                if state != "yes":
                    continue

                icon = element.select_one("i")
                label = None
                if isinstance(icon, Tag):
                    label = icon.get("aria-label") or icon.get("area-label")
                if label and label != "予約可":
                    continue

                clipped_start = max(segment_start, clock_to_minutes(MONITOR_START))
                clipped_end = min(segment_end, clock_to_minutes(MONITOR_END))
                if clipped_end <= clipped_start:
                    continue
                raw_slots.append(
                    make_availability_slot(
                        KAMOIKE_FACILITY_ID,
                        KAMOIKE_FACILITY_NAME,
                        target.date.isoformat(),
                        court_name,
                        clipped_start,
                        clipped_end,
                        reservation_url,
                    )
                )

    if row_count == 0:
        raise ScrapeStructureError(
            "unexpected_dom",
            "The schedule has no visible court rows",
        )

    availability = [
        slot
        for slot in merge_consecutive_slots(raw_slots)
        if slot["duration_minutes"] >= MINIMUM_DURATION_MINUTES
    ]
    return success_result(target, checked_at, reservation_url, availability)


def success_result(
    target: TargetDay,
    checked_at: str,
    reservation_url: str,
    availability: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "date": target.date.isoformat(),
        "day_type": target.day_type,
        "holiday_name": target.holiday_name,
        "status": "success",
        "error_type": None,
        "error_message": None,
        "checked_at": checked_at,
        "reservation_url": reservation_url,
        "availability": availability,
    }


def error_result(
    target: TargetDay,
    checked_at: str,
    reservation_url: str,
    error_type: str,
    error_message: str,
) -> dict[str, Any]:
    return {
        "date": target.date.isoformat(),
        "day_type": target.day_type,
        "holiday_name": target.holiday_name,
        "status": "error",
        "error_type": error_type,
        "error_message": error_message,
        "checked_at": checked_at,
        "reservation_url": reservation_url,
        "availability": [],
    }


def pending_result(
    target: TargetDay,
    reservation_url: str,
    message: str,
) -> dict[str, Any]:
    return {
        "date": target.date.isoformat(),
        "day_type": target.day_type,
        "holiday_name": target.holiday_name,
        "status": "selector_pending",
        "error_type": None,
        "error_message": message,
        "checked_at": datetime.now(JST).isoformat(timespec="seconds"),
        "reservation_url": reservation_url,
        "availability": [],
    }


def scrape_kamoike(
    client: PageClient,
    facility: Facility,
    target: TargetDay,
) -> dict[str, Any]:
    reservation_url = facility.url_template.format(date=target.date.isoformat())
    capture = client.capture_page(
        reservation_url,
        SNAPSHOT_ROOT / KAMOIKE_FACILITY_ID,
        target.date.isoformat(),
    )
    if capture.error_type:
        return error_result(
            target,
            capture.checked_at,
            reservation_url,
            capture.error_type,
            capture.error_message or capture.error_type,
        )
    try:
        return parse_kamoike_html(
            capture.html,
            target,
            reservation_url,
            capture.checked_at,
        )
    except ScrapeStructureError as exc:
        return error_result(
            target,
            capture.checked_at,
            reservation_url,
            exc.error_type,
            str(exc),
        )


def _scrape_with_selector(
    client: PageClient,
    facility: Facility,
    target: TargetDay,
) -> dict[str, Any]:
    selector = os.getenv(facility.selector_env, "").strip()
    reservation_url = (
        facility.url_template.format(date=target.date.isoformat())
        if facility.url_template
        else ""
    )
    if not facility.url_template:
        return pending_result(target, reservation_url, "URL template is not configured")
    if not selector:
        return pending_result(
            target,
            reservation_url,
            f"{facility.selector_env} is not configured",
        )
    try:
        texts = client.extract_texts(reservation_url, selector)
    except Exception as exc:
        return error_result(
            target,
            datetime.now(JST).isoformat(timespec="seconds"),
            reservation_url,
            "navigation_error",
            str(exc),
        )
    # SuMIzei remains a separate adapter pending its live-DOM implementation.
    if texts:
        return pending_result(
            target,
            reservation_url,
            "SuMIzei DOM parser is not implemented",
        )
    return pending_result(target, reservation_url, "No selected elements were found")


def scrape_sumizei(
    client: PageClient,
    facility: Facility,
    target: TargetDay,
) -> dict[str, Any]:
    return _scrape_with_selector(client, facility, target)


def configured_facilities() -> tuple[Facility, ...]:
    return (
        Facility(
            id=KAMOIKE_FACILITY_ID,
            name=KAMOIKE_FACILITY_NAME,
            url_template=KAMOIKE_URL_TEMPLATE,
            selector_env="",
            scraper=scrape_kamoike,
            requires_browser=True,
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
        "schema_version": 2,
        "generated_at": None,
        "window": {
            "days": WINDOW_DAYS,
            "start": MONITOR_START.strftime("%H:%M"),
            "end": MONITOR_END.strftime("%H:%M"),
            "minimum_duration_minutes": MINIMUM_DURATION_MINUTES,
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
        facility.requires_browser
        or (
            facility.url_template
            and facility.selector_env
            and os.getenv(facility.selector_env, "").strip()
        )
        for facility in selected_facilities
    )
    client_context: Any = client_factory() if needs_browser else _NoopClientContext()

    facility_results: list[dict[str, Any]] = []
    with client_context as client:
        for facility in selected_facilities:
            dates: list[dict[str, Any]] = []
            for target in targets:
                try:
                    dates.append(facility.scraper(client, facility, target))
                except Exception as exc:
                    reservation_url = (
                        facility.url_template.format(date=target.date.isoformat())
                        if facility.url_template
                        else ""
                    )
                    dates.append(
                        error_result(
                            target,
                            datetime.now(JST).isoformat(timespec="seconds"),
                            reservation_url,
                            "unexpected_error",
                            str(exc),
                        )
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

    def capture_page(
        self,
        url: str,
        snapshot_directory: Path,
        snapshot_name: str,
    ) -> PageCapture:
        raise RuntimeError(f"Browser was not configured for {url}")

    def extract_texts(self, url: str, selector: str) -> list[str]:
        raise RuntimeError(f"Browser was not configured for {url} ({selector})")


def available_slot_keys(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    keys: dict[str, dict[str, Any]] = {}
    for facility in document.get("facilities", []):
        for date_entry in facility.get("dates", []):
            slots = date_entry.get("availability", date_entry.get("slots", []))
            for slot in slots:
                if slot.get("status") != "available":
                    continue
                slot_id = slot.get("slot_id")
                if not slot_id:
                    slot_id = make_slot_id(
                        str(slot.get("facility_id", facility.get("id", ""))),
                        str(slot.get("date", date_entry.get("date", ""))),
                        str(slot.get("court_name", slot.get("court", ""))),
                        str(slot.get("start_time", slot.get("start", ""))),
                        str(slot.get("end_time", slot.get("end", ""))),
                    )
                keys[str(slot_id)] = dict(slot)
    return keys


def detect_new_availability(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> list[dict[str, Any]]:
    old_keys = available_slot_keys(previous)
    current_keys = available_slot_keys(current)
    return [current_keys[key] for key in sorted(current_keys.keys() - old_keys.keys())]


def build_line_message(changes: list[dict[str, Any]]) -> str:
    lines = ["【テニスコート空き情報】", "新しい空き候補が見つかりました。"]
    for change in changes[:10]:
        lines.append(
            f"・{change['facility_name']} {change['date']} "
            f"{change['start_time']}〜{change['end_time']} "
            f"{change['court_name']}"
        )
    if len(changes) > 10:
        lines.append(f"ほか {len(changes) - 10} 件")
    return "\n".join(lines)[:4900]


def send_line_notification(
    changes: list[dict[str, Any]],
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
    comparable = json.loads(json.dumps(document))
    comparable.pop("generated_at", None)
    for facility in comparable.get("facilities", []):
        for date_entry in facility.get("dates", []):
            date_entry.pop("checked_at", None)
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
    print(f"target_days={len(targets)} new_slots={len(changes)} data={result}")
    send_line_notification(changes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
