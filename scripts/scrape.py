from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import urllib.error
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
NOTIFICATION_STATE_PATH = Path("data/notification-state.json")
RUN_OUTPUT_DIRECTORY = Path("run-output")
SNAPSHOT_ROOT = Path("snapshots")
# LINE Messaging API: text max 5000 UTF-16 units, push max 5 messages/request.
LINE_TEXT_MAX_UTF16_UNITS = 5000
LINE_MESSAGES_PER_REQUEST = 5
LINE_REQUEST_TIMEOUT_SECONDS = 20
LINE_TEST_MESSAGE = "鹿児島テニス空き通知の接続テストです。"

KAMOIKE_FACILITY_ID = "kamoike-prefectural"
KAMOIKE_FACILITY_NAME = "鴨池県営テニスコート"
KAMOIKE_URL_TEMPLATE = (
    "https://v2.spm-cloud.com/user/kamoike-undo/reserves/daily"
    "?date={date}&category_id=483&area_id=289"
)

SUMIZEI_FACILITY_ID = "sumizei"
SUMIZEI_FACILITY_NAME = "SuMIzeiテニスコート"
P_KASHIKAN_BASE_URL = "https://k2.p-kashikan.jp/kagoshima-city/index.php"
SUMIZEI_BASE_URL = P_KASHIKAN_BASE_URL
SUMIZEI_FACILITY_CODE = "029"

TOUKAI_FACILITY_ID = "toukai-tennis"
TOUKAI_FACILITY_NAME = "東開庭球場"
TOUKAI_FACILITY_CODE = "131"

WIDTH_PATTERN = re.compile(r"width\s*:\s*([\d.]+)%", re.IGNORECASE)
STATE_CLASS_PATTERN = re.compile(r"rsv--result--(yes|no|out)")
PIXEL_WIDTH_PATTERN = re.compile(r"width\s*:\s*([\d.]+)px", re.IGNORECASE)
P_KASHIKAN_SLOT_PATTERN = re.compile(
    r"setAppStatus\(\s*'(?P<resource>[^']+)'\s*,\s*"
    r"'(?P<date>\d{4}/\d{2}/\d{2})'\s*,\s*\d+\s*,\s*"
    r"'(?P<times>\d{8})'"
)
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
    p_kashikan_code: str | None = None


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

    def capture_p_kashikan_schedule(
        self,
        reservation_url: str,
        target_date: date,
        snapshot_directory: Path,
        facility_code: str,
        facility_name: str,
    ) -> PageCapture: ...


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

    @staticmethod
    def _save_page_snapshot(
        page: Any,
        snapshot_directory: Path,
        snapshot_name: str,
    ) -> tuple[str, str | None]:
        snapshot_directory.mkdir(parents=True, exist_ok=True)
        html_path = snapshot_directory / f"{snapshot_name}.html"
        image_path = snapshot_directory / f"{snapshot_name}.png"
        errors: list[str] = []
        try:
            html = page.content()
        except Exception as exc:
            html = (
                "<!doctype html><html><body><h1>Capture failed</h1>"
                f"<pre>{exc}</pre></body></html>"
            )
            errors.append(f"content capture failed: {exc}")
        html_path.write_text(html, encoding="utf-8")
        try:
            page.screenshot(path=str(image_path), full_page=True)
        except Exception as exc:
            errors.append(f"screenshot failed: {exc}")
        return html, "; ".join(errors) or None

    def capture_p_kashikan_schedule(
        self,
        reservation_url: str,
        target_date: date,
        snapshot_directory: Path,
        facility_code: str,
        facility_name: str,
    ) -> PageCapture:
        """Follow the anonymous public form flow observed on the live site."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        if self._browser is None:
            raise RuntimeError("PlaywrightClient must be used as a context manager")

        checked_at = datetime.now(JST).isoformat(timespec="seconds")
        date_label = target_date.isoformat()
        ymd = target_date.strftime("%Y%m%d")
        response_status: int | None = None
        html = ""
        step = "top"
        context = self._browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 1440, "height": 1000},
        )
        page = context.new_page()
        try:
            response = page.goto(
                reservation_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            response_status = response.status if response else None
            html, snapshot_error = self._save_page_snapshot(
                page, snapshot_directory, f"{date_label}-top"
            )
            if snapshot_error:
                raise ScrapeStructureError("snapshot_error", snapshot_error)
            if response_status in {401, 403, 429} or any(
                marker in BeautifulSoup(html, "html.parser").get_text(" ", strip=True).lower()
                for marker in ACCESS_DENIED_MARKERS
            ):
                raise ScrapeStructureError(
                    "access_denied", f"Access denied (HTTP {response_status})"
                )

            step = "facility-search"
            with page.expect_navigation(wait_until="domcontentloaded", timeout=60_000) as nav:
                page.get_by_role("link", name="施設 の空きを見る").click()
            response = nav.value
            response_status = response.status if response else response_status
            page.locator('input[name="ShisetsuCode"]').first.wait_for(
                state="attached", timeout=30_000
            )
            html, snapshot_error = self._save_page_snapshot(
                page, snapshot_directory, f"{date_label}-facility-search"
            )
            if snapshot_error:
                raise ScrapeStructureError("snapshot_error", snapshot_error)
            facility_radio = page.locator(f"#scd{facility_code}")
            if facility_radio.count() != 1:
                raise ScrapeStructureError(
                    "facility_not_found",
                    f"{facility_name} facility code {facility_code} was not found",
                )

            step = "facility-selected"
            with page.expect_navigation(wait_until="domcontentloaded", timeout=60_000) as nav:
                facility_radio.click()
            response = nav.value
            response_status = response.status if response else response_status
            page.locator('input[name="UseDate"]').first.wait_for(
                state="attached", timeout=30_000
            )
            html, snapshot_error = self._save_page_snapshot(
                page, snapshot_directory, f"{date_label}-facility-selected"
            )
            if snapshot_error:
                raise ScrapeStructureError("snapshot_error", snapshot_error)

            step = "schedule"
            with page.expect_navigation(wait_until="domcontentloaded", timeout=60_000) as nav:
                page.evaluate(
                    """({ ymd, facilityCode }) => {
                        const form = document.forms.forma;
                        if (!form || !form.elements.UseDate || !form.elements.ShisetsuCode) {
                            throw new Error('Expected public availability form is missing');
                        }
                        form.elements.UseYM.value = ymd.slice(0, 6);
                        form.elements.UseDay.value = String(Number(ymd.slice(6, 8)));
                        form.elements.UseDate.value = ymd;
                        form.elements.ShisetsuCode.value = facilityCode;
                        form.elements.disp_span.value = '0';
                        form.submit();
                    }""",
                    {"ymd": ymd, "facilityCode": facility_code},
                )
            response = nav.value
            response_status = response.status if response else response_status
            try:
                page.locator(f'input[name="UseDate"][value="{ymd}"]').wait_for(
                    state="attached", timeout=30_000
                )
            except PlaywrightTimeoutError as exc:
                raise ScrapeStructureError(
                    "date_selection_failed",
                    f"The schedule did not switch to {date_label}",
                ) from exc
            try:
                page.locator(".SelectCalendar table.koma-table td.name").first.wait_for(
                    state="attached", timeout=30_000
                )
            except PlaywrightTimeoutError as exc:
                raise ScrapeStructureError(
                    "no_schedule_table",
                    f"No {facility_name} court schedule was found",
                ) from exc
            html, snapshot_error = self._save_page_snapshot(
                page, snapshot_directory, f"{date_label}-schedule"
            )
            if snapshot_error:
                raise ScrapeStructureError("snapshot_error", snapshot_error)
            return PageCapture(
                html=html,
                checked_at=checked_at,
                response_status=response_status,
            )
        except ScrapeStructureError as exc:
            html, snapshot_error = self._save_page_snapshot(
                page, snapshot_directory, f"{date_label}-{step}-error"
            )
            message = str(exc)
            if snapshot_error:
                message = f"{message}; {snapshot_error}"
            return PageCapture(
                html=html,
                checked_at=checked_at,
                response_status=response_status,
                error_type=exc.error_type,
                error_message=message,
            )
        except PlaywrightTimeoutError as exc:
            html, snapshot_error = self._save_page_snapshot(
                page, snapshot_directory, f"{date_label}-{step}-error"
            )
            error_type = "navigation_timeout"
            if step == "schedule":
                error_type = "date_selection_failed"
            message = str(exc)
            if snapshot_error:
                message = f"{message}; {snapshot_error}"
            return PageCapture(
                html=html,
                checked_at=checked_at,
                response_status=response_status,
                error_type=error_type,
                error_message=message,
            )
        except Exception as exc:
            html, snapshot_error = self._save_page_snapshot(
                page, snapshot_directory, f"{date_label}-{step}-error"
            )
            message = str(exc)
            if snapshot_error:
                message = f"{message}; {snapshot_error}"
            return PageCapture(
                html=html,
                checked_at=checked_at,
                response_status=response_status,
                error_type="navigation_error",
                error_message=message,
            )
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


def _pixel_width(element: Tag) -> float:
    match = PIXEL_WIDTH_PATTERN.search(element.get("style", ""))
    if not match:
        raise ScrapeStructureError(
            "unexpected_dom", "A P-Kashikan schedule cell has no pixel width"
        )
    return float(match.group(1))


def _normalize_p_kashikan_boundary(minutes: int) -> int:
    """Convert P-Kashikan's inclusive :29/:59 values to displayed boundaries."""
    return minutes + 1 if minutes % 60 in {29, 59} else minutes


def _p_kashikan_cell_minutes(
    element: Tag,
    inferred_start: int,
    inferred_end: int,
    target: TargetDay,
    facility_code: str,
    facility_name: str,
) -> tuple[int, int]:
    handler = element.get("onmousedown", "")
    if not handler:
        return (
            _normalize_p_kashikan_boundary(inferred_start),
            _normalize_p_kashikan_boundary(inferred_end),
        )
    match = P_KASHIKAN_SLOT_PATTERN.search(handler)
    if not match:
        raise ScrapeStructureError(
            "unexpected_dom",
            f"An available {facility_name} cell has an unknown handler",
        )
    if not match.group("resource").startswith(f"{facility_code}|"):
        raise ScrapeStructureError(
            "unexpected_dom", "An available cell belongs to another facility"
        )
    expected_date = target.date.strftime("%Y/%m/%d")
    if match.group("date") != expected_date:
        raise ScrapeStructureError(
            "date_selection_failed",
            f"Expected {expected_date}, got {match.group('date')}",
        )
    time_range = match.group("times")
    start = _normalize_p_kashikan_boundary(
        clock_to_minutes(f"{time_range[0:2]}:{time_range[2:4]}")
    )
    end = _normalize_p_kashikan_boundary(
        clock_to_minutes(f"{time_range[4:6]}:{time_range[6:8]}")
    )
    if end <= start:
        raise ScrapeStructureError(
            "unexpected_dom",
            f"Invalid {facility_name} cell time range: {time_range}",
        )
    return start, end


def parse_p_kashikan_html(
    html: str,
    target: TargetDay,
    reservation_url: str,
    checked_at: str,
    facility_id: str,
    facility_name: str,
    facility_code: str,
) -> dict[str, Any]:
    """Parse the live P-KASHIKAN court grid without scanning legends."""
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True)
    if any(marker in page_text.lower() for marker in ACCESS_DENIED_MARKERS):
        raise ScrapeStructureError("access_denied", "Access denied page detected")

    facility_input = soup.select_one(
        f'input[name="ShisetsuCode"][value="{facility_code}"]'
    )
    if facility_input is None:
        raise ScrapeStructureError(
            "facility_not_found",
            f"{facility_name} facility code {facility_code} was not found",
        )
    if not facility_input.has_attr("checked"):
        raise ScrapeStructureError(
            "unexpected_dom", f"{facility_name} is not the selected facility"
        )

    use_date = soup.select_one('input[name="UseDate"]')
    expected_ymd = target.date.strftime("%Y%m%d")
    if not isinstance(use_date, Tag) or use_date.get("value") != expected_ymd:
        raise ScrapeStructureError(
            "date_selection_failed",
            f"Expected UseDate={expected_ymd}",
        )

    normalized_text = unicodedata.normalize("NFKC", page_text)
    if unicodedata.normalize("NFKC", facility_name) not in normalized_text:
        raise ScrapeStructureError(
            "unexpected_dom", "The selected facility heading is missing"
        )

    calendar = soup.select_one(".SelectCalendar")
    if not isinstance(calendar, Tag) or _is_hidden(calendar):
        raise ScrapeStructureError(
            "no_schedule_table", "No visible .SelectCalendar schedule was found"
        )
    header = calendar.select_one("table.koma-table th.header")
    if not isinstance(header, Tag) or header.get_text(" ", strip=True) != "施設":
        raise ScrapeStructureError(
            "unexpected_dom", f"The {facility_name} time header is missing"
        )
    header_cells = header.find_parent("tr").find_all("th", recursive=False)
    try:
        hour_labels = [int(cell.get_text(" ", strip=True)) for cell in header_cells[1:]]
    except ValueError as exc:
        raise ScrapeStructureError(
            "unexpected_dom", f"The {facility_name} time header is invalid"
        ) from exc
    if len(hour_labels) < 2 or hour_labels != list(
        range(hour_labels[0], hour_labels[-1] + 1)
    ):
        raise ScrapeStructureError(
            "unexpected_dom",
            f"Unexpected {facility_name} time header: {hour_labels}",
        )
    grid_start = hour_labels[0] * 60
    grid_end = (hour_labels[-1] + 1) * 60

    availability_candidates: list[dict[str, Any]] = []
    row_count = 0
    for table in calendar.select("table.koma-table"):
        if not isinstance(table, Tag) or _is_hidden(table):
            continue
        row = table.select_one("tr")
        if not isinstance(row, Tag):
            continue
        court_element = row.select_one("td.name")
        if not isinstance(court_element, Tag):
            continue
        court_name = court_element.get_text(" ", strip=True)
        if not court_name:
            raise ScrapeStructureError(
                "unexpected_dom", f"A {facility_name} court row has no court name"
            )
        cells = [
            cell
            for cell in row.find_all("td", recursive=False)
            if isinstance(cell, Tag) and cell is not court_element and not _is_hidden(cell)
        ]
        if not cells:
            raise ScrapeStructureError(
                "unexpected_dom", f"No state cells for {court_name}"
            )
        widths = [_pixel_width(cell) for cell in cells]
        total_width = sum(widths)
        if total_width <= 0:
            raise ScrapeStructureError(
                "unexpected_dom", f"Invalid cell widths for {court_name}"
            )

        row_count += 1
        row_slots: list[dict[str, Any]] = []
        elapsed_width = 0.0
        grid_minutes = grid_end - grid_start
        for cell, width in zip(cells, widths, strict=True):
            inferred_start = grid_start + round(
                elapsed_width / total_width * grid_minutes
            )
            elapsed_width += width
            inferred_end = grid_start + round(
                elapsed_width / total_width * grid_minutes
            )
            marker = cell.get_text(" ", strip=True)
            if marker not in {"●", "○", "〇"}:
                continue
            if marker in {"○", "〇"} and not cell.get("onmousedown"):
                raise ScrapeStructureError(
                    "unexpected_dom",
                    f"An internet-available cell for {court_name} has no time data",
                )
            segment_start, segment_end = _p_kashikan_cell_minutes(
                cell,
                inferred_start,
                inferred_end,
                target,
                facility_code,
                facility_name,
            )
            clipped_start = max(segment_start, clock_to_minutes(MONITOR_START))
            clipped_end = min(segment_end, clock_to_minutes(MONITOR_END))
            if clipped_end <= clipped_start:
                continue
            row_slots.append(
                make_availability_slot(
                    facility_id,
                    facility_name,
                    target.date.isoformat(),
                    court_name,
                    clipped_start,
                    clipped_end,
                    reservation_url,
                )
            )
        availability_candidates.extend(merge_consecutive_slots(row_slots))

    if row_count == 0:
        raise ScrapeStructureError(
            "unexpected_dom", f"The {facility_name} schedule has no visible court rows"
        )
    availability_by_id = {
        slot["slot_id"]: slot
        for slot in availability_candidates
        if slot["duration_minutes"] >= MINIMUM_DURATION_MINUTES
    }
    availability = [
        slot
        for _, slot in sorted(
            availability_by_id.items(),
            key=lambda item: (
                natural_sort_key(item[1]["court_name"]),
                item[1]["start_time"],
                item[1]["end_time"],
            ),
        )
    ]
    return success_result(target, checked_at, reservation_url, availability)


def parse_sumizei_html(
    html: str,
    target: TargetDay,
    reservation_url: str,
    checked_at: str,
) -> dict[str, Any]:
    return parse_p_kashikan_html(
        html,
        target,
        reservation_url,
        checked_at,
        SUMIZEI_FACILITY_ID,
        SUMIZEI_FACILITY_NAME,
        SUMIZEI_FACILITY_CODE,
    )


def scrape_p_kashikan(
    client: PageClient,
    facility: Facility,
    target: TargetDay,
) -> dict[str, Any]:
    reservation_url = facility.url_template
    if not facility.p_kashikan_code:
        return error_result(
            target,
            datetime.now(JST).isoformat(timespec="seconds"),
            reservation_url,
            "facility_not_found",
            f"{facility.name} has no P-Kashikan facility code",
        )
    capture = client.capture_p_kashikan_schedule(
        reservation_url,
        target.date,
        SNAPSHOT_ROOT / facility.id,
        facility.p_kashikan_code,
        facility.name,
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
        return parse_p_kashikan_html(
            capture.html,
            target,
            reservation_url,
            capture.checked_at,
            facility.id,
            facility.name,
            facility.p_kashikan_code,
        )
    except ScrapeStructureError as exc:
        return error_result(
            target,
            capture.checked_at,
            reservation_url,
            exc.error_type,
            str(exc),
        )


def scrape_sumizei(
    client: PageClient,
    facility: Facility,
    target: TargetDay,
) -> dict[str, Any]:
    return scrape_p_kashikan(client, facility, target)


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
            id=SUMIZEI_FACILITY_ID,
            name=SUMIZEI_FACILITY_NAME,
            url_template=P_KASHIKAN_BASE_URL,
            selector_env="",
            scraper=scrape_p_kashikan,
            requires_browser=True,
            p_kashikan_code=SUMIZEI_FACILITY_CODE,
        ),
        Facility(
            id=TOUKAI_FACILITY_ID,
            name=TOUKAI_FACILITY_NAME,
            url_template=P_KASHIKAN_BASE_URL,
            selector_env="",
            scraper=scrape_p_kashikan,
            requires_browser=True,
            p_kashikan_code=TOUKAI_FACILITY_CODE,
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

    def capture_p_kashikan_schedule(
        self,
        reservation_url: str,
        target_date: date,
        snapshot_directory: Path,
        facility_code: str,
        facility_name: str,
    ) -> PageCapture:
        raise RuntimeError(
            f"Browser was not configured for {facility_name} ({reservation_url})"
        )


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
    previous_statuses: dict[tuple[str, str], str] = {}
    for facility in previous.get("facilities", []):
        facility_id = str(facility.get("id", ""))
        for date_entry in facility.get("dates", []):
            previous_statuses[(facility_id, str(date_entry.get("date", "")))] = str(
                date_entry.get("status", "")
            )

    changes: list[dict[str, Any]] = []
    for key in sorted(current_keys.keys() - old_keys.keys()):
        slot = current_keys[key]
        scope = (str(slot.get("facility_id", "")), str(slot.get("date", "")))
        previous_status = previous_statuses.get(scope)
        if previous_status and previous_status != "success":
            continue
        changes.append(slot)
    return changes


@dataclass(frozen=True)
class LineSendResult:
    attempted: bool
    succeeded: bool
    status: str
    request_count: int = 0
    error_message: str | None = None


@dataclass(frozen=True)
class RunOptions:
    dry_run: bool = False
    send_notification: bool = False
    test_notification: bool = False
    initialize_notification_baseline: bool = False


@dataclass(frozen=True)
class RunResult:
    availability_written: bool
    notification_state_written: bool
    notification_status: str
    notification_candidates: int
    line_result: LineSendResult | None


def utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def japanese_date_label(value: str) -> str:
    parsed = date.fromisoformat(value)
    weekdays = ("月", "火", "水", "木", "金", "土", "日")
    return f"{parsed.month}月{parsed.day}日（{weekdays[parsed.weekday()]}）"


def _line_group_sections(
    changes: list[dict[str, Any]],
    max_units: int,
) -> list[str]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for change in sorted(
        changes,
        key=lambda item: (
            item["facility_name"],
            item["date"],
            natural_sort_key(item["court_name"]),
            item["start_time"],
            item["end_time"],
        ),
    ):
        key = (
            str(change["facility_name"]),
            str(change["date"]),
            str(change["reservation_url"]),
        )
        grouped.setdefault(key, []).append(change)

    sections: list[str] = []
    for (facility_name, target_date, reservation_url), slots in grouped.items():
        prefix = f"{facility_name}\n{japanese_date_label(target_date)}"
        suffix = f"予約ページ:\n{reservation_url}"
        entries = [
            f"{slot['court_name']}\n{slot['start_time']}〜{slot['end_time']}"
            for slot in slots
        ]
        current_entries: list[str] = []
        for entry in entries:
            candidate_entries = [*current_entries, entry]
            candidate = f"{prefix}\n\n" + "\n\n".join(candidate_entries)
            candidate += f"\n\n{suffix}"
            if utf16_units(candidate) <= max_units:
                current_entries = candidate_entries
                continue
            if not current_entries:
                raise ValueError("A single LINE availability entry exceeds the text limit")
            sections.append(
                f"{prefix}\n\n" + "\n\n".join(current_entries) + f"\n\n{suffix}"
            )
            current_entries = [entry]
        if current_entries:
            sections.append(
                f"{prefix}\n\n" + "\n\n".join(current_entries) + f"\n\n{suffix}"
            )
    return sections


def build_line_messages(
    changes: list[dict[str, Any]],
    max_units: int = LINE_TEXT_MAX_UTF16_UNITS,
) -> list[str]:
    if not changes:
        return []
    heading = "【鹿児島テニス空き情報】"
    section_limit = max_units - utf16_units(heading) - 2
    sections = _line_group_sections(changes, section_limit)
    messages: list[str] = []
    current = heading
    for section in sections:
        candidate = f"{current}\n\n{section}"
        if utf16_units(candidate) <= max_units:
            current = candidate
            continue
        messages.append(current)
        current = f"{heading}\n\n{section}"
        if utf16_units(current) > max_units:
            raise ValueError("A LINE message exceeds the text limit")
    if current != heading:
        messages.append(current)
    return messages


def build_line_message(changes: list[dict[str, Any]]) -> str:
    messages = build_line_messages(changes)
    return messages[0] if messages else ""


def _message_batches(messages: list[str]) -> Iterable[list[str]]:
    for index in range(0, len(messages), LINE_MESSAGES_PER_REQUEST):
        yield messages[index : index + LINE_MESSAGES_PER_REQUEST]


def send_line_messages(
    messages: list[str],
    token: str | None = None,
    user_id: str | None = None,
    opener: Callable[..., Any] | None = None,
) -> LineSendResult:
    if not messages:
        return LineSendResult(False, False, "no_messages")
    line_token = token or os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    line_user_id = user_id or os.getenv("LINE_USER_ID")
    if not line_token or not line_user_id:
        print("LINE credentials are missing; notification skipped.")
        return LineSendResult(False, False, "missing_credentials")

    request_opener = opener or urllib.request.urlopen
    request_count = 0
    try:
        for batch in _message_batches(messages):
            payload = {
                "to": line_user_id,
                "messages": [{"type": "text", "text": message} for message in batch],
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
            request_count += 1
            with request_opener(
                request, timeout=LINE_REQUEST_TIMEOUT_SECONDS
            ) as response:
                status = int(response.status)
                if not 200 <= status < 300:
                    return LineSendResult(
                        True,
                        False,
                        "http_error",
                        request_count,
                        f"HTTP {status}",
                    )
        print(f"LINE notification succeeded: requests={request_count}")
        return LineSendResult(True, True, "success", request_count)
    except urllib.error.HTTPError as exc:
        return LineSendResult(
            True, False, "http_error", request_count, f"HTTP {exc.code}"
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        status = "timeout" if isinstance(exc, TimeoutError) else "network_error"
        return LineSendResult(
            True,
            False,
            status,
            request_count,
            type(exc).__name__,
        )


def send_line_notification_result(
    changes: list[dict[str, Any]],
    token: str | None = None,
    user_id: str | None = None,
    opener: Callable[..., Any] | None = None,
) -> LineSendResult:
    try:
        messages = build_line_messages(changes)
    except (KeyError, TypeError, ValueError):
        return LineSendResult(False, False, "message_format_error")
    return send_line_messages(messages, token=token, user_id=user_id, opener=opener)


def send_line_notification(
    changes: list[dict[str, Any]],
    token: str | None = None,
    user_id: str | None = None,
) -> bool:
    return send_line_notification_result(changes, token, user_id).succeeded


def send_line_test_notification(
    token: str | None = None,
    user_id: str | None = None,
    opener: Callable[..., Any] | None = None,
) -> LineSendResult:
    return send_line_messages(
        [LINE_TEST_MESSAGE], token=token, user_id=user_id, opener=opener
    )


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


def empty_notification_state() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "initialized": False,
        "initialized_facility_ids": [],
        "updated_at": None,
        "observed_slot_ids": [],
        "observed_slot_scopes": {},
        "last_notification_status": None,
    }


def load_notification_state(
    path: Path = NOTIFICATION_STATE_PATH,
) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("state is not an object")
        initialized = raw.get("initialized")
        initialized_facility_ids = raw.get("initialized_facility_ids")
        slot_ids = raw.get("observed_slot_ids")
        scopes = raw.get("observed_slot_scopes", {})
        if not isinstance(initialized, bool):
            raise ValueError("initialized is not boolean")
        if initialized_facility_ids is not None and (
            not isinstance(initialized_facility_ids, list)
            or not all(
                isinstance(facility_id, str)
                for facility_id in initialized_facility_ids
            )
        ):
            raise ValueError("initialized_facility_ids is invalid")
        if not isinstance(slot_ids, list) or not all(
            isinstance(slot_id, str) for slot_id in slot_ids
        ):
            raise ValueError("observed_slot_ids is invalid")
        if not isinstance(scopes, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in scopes.items()
        ):
            raise ValueError("observed_slot_scopes is invalid")
        return {
            "schema_version": 2,
            "initialized": initialized,
            "initialized_facility_ids": (
                sorted(set(initialized_facility_ids))
                if initialized_facility_ids is not None
                else None
            ),
            "updated_at": raw.get("updated_at"),
            "observed_slot_ids": sorted(set(slot_ids)),
            "observed_slot_scopes": {
                key: scopes[key] for key in sorted(scopes) if key in slot_ids
            },
            "last_notification_status": raw.get("last_notification_status"),
        }
    except FileNotFoundError:
        return empty_notification_state()
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        print("Notification state is missing or invalid; baseline initialization required.")
        return empty_notification_state()


def write_notification_state(
    state: dict[str, Any],
    path: Path = NOTIFICATION_STATE_PATH,
) -> None:
    write_document(state, path)


def slot_scope(slot: dict[str, Any]) -> str:
    return f"{slot.get('facility_id', '')}|{slot.get('date', '')}"


def document_date_statuses(document: dict[str, Any]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for facility in document.get("facilities", []):
        facility_id = str(facility.get("id", ""))
        for date_entry in facility.get("dates", []):
            scope = f"{facility_id}|{date_entry.get('date', '')}"
            statuses[scope] = str(date_entry.get("status", ""))
    return statuses


def document_facility_ids(document: dict[str, Any]) -> set[str]:
    return {
        str(facility.get("id", ""))
        for facility in document.get("facilities", [])
        if facility.get("id")
    }


def successful_facility_ids(document: dict[str, Any]) -> set[str]:
    return {
        str(facility.get("id", ""))
        for facility in document.get("facilities", [])
        if facility.get("id")
        and any(
            date_entry.get("status") == "success"
            for date_entry in facility.get("dates", [])
        )
    }


def state_initialized_facility_ids(
    state: dict[str, Any],
    previous_availability: dict[str, Any],
) -> set[str]:
    configured = state.get("initialized_facility_ids")
    if configured is not None:
        return set(configured)
    if state.get("initialized", False):
        return document_facility_ids(previous_availability)
    return set()


def _p_kashikan_slot_time_migration_matches(
    previous_slot: dict[str, Any],
    current_slot: dict[str, Any],
) -> bool:
    facility_id = str(previous_slot.get("facility_id", ""))
    if facility_id not in {SUMIZEI_FACILITY_ID, TOUKAI_FACILITY_ID}:
        return False
    if any(
        str(previous_slot.get(field, "")) != str(current_slot.get(field, ""))
        for field in ("facility_id", "date", "court_name")
    ):
        return False
    try:
        previous_start = clock_to_minutes(str(previous_slot.get("start_time", "")))
        previous_end = clock_to_minutes(str(previous_slot.get("end_time", "")))
        current_start = clock_to_minutes(str(current_slot.get("start_time", "")))
        current_end = clock_to_minutes(str(current_slot.get("end_time", "")))
    except (TypeError, ValueError, OverflowError):
        return False
    return (
        (previous_start, previous_end) != (current_start, current_end)
        and _normalize_p_kashikan_boundary(previous_start) == current_start
        and _normalize_p_kashikan_boundary(previous_end) == current_end
    )


def migrate_p_kashikan_observed_ids(
    observed_ids: set[str],
    observed_scopes: dict[str, str],
    previous_availability: dict[str, Any],
    current_availability: dict[str, Any],
) -> tuple[set[str], dict[str, str]]:
    """Move observed IDs to corrected P-Kashikan times without notifying again."""
    previous_slots = available_slot_keys(previous_availability)
    current_slots = available_slot_keys(current_availability)
    migrated_ids = set(observed_ids)
    migrated_scopes = dict(observed_scopes)
    claimed_current_ids: set[str] = set()

    for previous_id in sorted(observed_ids):
        previous_slot = previous_slots.get(previous_id)
        if previous_slot is None:
            continue
        for current_id, current_slot in sorted(current_slots.items()):
            if current_id in claimed_current_ids:
                continue
            if not _p_kashikan_slot_time_migration_matches(
                previous_slot, current_slot
            ):
                continue
            migrated_ids.remove(previous_id)
            migrated_ids.add(current_id)
            scope = migrated_scopes.pop(previous_id, slot_scope(previous_slot))
            migrated_scopes[current_id] = scope
            claimed_current_ids.add(current_id)
            break
    return migrated_ids, migrated_scopes


@dataclass(frozen=True)
class NotificationObservation:
    candidates: list[dict[str, Any]]
    suppressed_recovery_ids: set[str]
    suppressed_initial_ids: set[str]
    target_ids: set[str]
    target_scopes: dict[str, str]
    target_facility_ids: set[str]
    failure_ids: set[str]
    failure_scopes: dict[str, str]
    failure_facility_ids: set[str]


def observe_notification_changes(
    state: dict[str, Any],
    previous_availability: dict[str, Any],
    current_availability: dict[str, Any],
) -> NotificationObservation:
    previous_ids = set(state.get("observed_slot_ids", []))
    previous_scopes = dict(state.get("observed_slot_scopes", {}))
    previous_ids, previous_scopes = migrate_p_kashikan_observed_ids(
        previous_ids,
        previous_scopes,
        previous_availability,
        current_availability,
    )
    current_slots = available_slot_keys(current_availability)
    current_statuses = document_date_statuses(current_availability)
    previous_statuses = document_date_statuses(previous_availability)
    initialized_facility_ids = state_initialized_facility_ids(
        state, previous_availability
    )
    target_facility_ids = (
        initialized_facility_ids | successful_facility_ids(current_availability)
    )
    newly_initialized_facility_ids = (
        target_facility_ids - initialized_facility_ids
    )

    target_ids = set(current_slots)
    target_scopes = {
        slot_id: slot_scope(slot) for slot_id, slot in current_slots.items()
    }
    failed_scopes = {
        scope for scope, status in current_statuses.items() if status != "success"
    }
    for slot_id in previous_ids:
        scope = previous_scopes.get(slot_id)
        if scope in failed_scopes:
            target_ids.add(slot_id)
            if scope:
                target_scopes[slot_id] = scope

    candidates: list[dict[str, Any]] = []
    suppressed_recovery_ids: set[str] = set()
    suppressed_initial_ids: set[str] = set()
    for slot_id in sorted(set(current_slots) - previous_ids):
        slot = current_slots[slot_id]
        scope = slot_scope(slot)
        if str(slot.get("facility_id", "")) in newly_initialized_facility_ids:
            suppressed_initial_ids.add(slot_id)
            continue
        previous_status = previous_statuses.get(scope)
        if previous_status and previous_status != "success":
            suppressed_recovery_ids.add(slot_id)
            continue
        candidates.append(slot)

    failure_ids = previous_ids | suppressed_recovery_ids | suppressed_initial_ids
    failure_scopes = dict(previous_scopes)
    for slot_id in suppressed_recovery_ids | suppressed_initial_ids:
        failure_scopes[slot_id] = target_scopes[slot_id]
    return NotificationObservation(
        candidates=candidates,
        suppressed_recovery_ids=suppressed_recovery_ids,
        suppressed_initial_ids=suppressed_initial_ids,
        target_ids=target_ids,
        target_scopes=target_scopes,
        target_facility_ids=target_facility_ids,
        failure_ids=failure_ids,
        failure_scopes=failure_scopes,
        failure_facility_ids=target_facility_ids,
    )


def updated_notification_state(
    previous: dict[str, Any],
    observed_ids: set[str],
    observed_scopes: dict[str, str],
    status: str,
    checked_at: str,
    initialized: bool = True,
    initialized_facility_ids: set[str] | None = None,
) -> dict[str, Any]:
    normalized_scopes = {
        slot_id: observed_scopes[slot_id]
        for slot_id in sorted(observed_ids)
        if slot_id in observed_scopes
    }
    candidate = {
        "schema_version": 2,
        "initialized": initialized,
        "initialized_facility_ids": sorted(
            initialized_facility_ids
            if initialized_facility_ids is not None
            else set(previous.get("initialized_facility_ids") or [])
        ),
        "updated_at": checked_at,
        "observed_slot_ids": sorted(observed_ids),
        "observed_slot_scopes": normalized_scopes,
        "last_notification_status": status,
    }
    comparable_previous = dict(previous)
    comparable_candidate = dict(candidate)
    comparable_previous.pop("updated_at", None)
    comparable_candidate.pop("updated_at", None)
    if comparable_previous == comparable_candidate:
        candidate["updated_at"] = previous.get("updated_at")
    return candidate


def process_scrape_result(
    previous: dict[str, Any],
    current: dict[str, Any],
    state: dict[str, Any],
    options: RunOptions,
    data_path: Path = DATA_PATH,
    state_path: Path = NOTIFICATION_STATE_PATH,
    output_directory: Path = RUN_OUTPUT_DIRECTORY,
    token: str | None = None,
    user_id: str | None = None,
    opener: Callable[..., Any] | None = None,
) -> RunResult:
    checked_at = datetime.now(JST).isoformat(timespec="seconds")
    observation = observe_notification_changes(state, previous, current)
    availability_changed = comparable_document(previous) != comparable_document(current)
    availability_written = False
    state_written = False
    line_result: LineSendResult | None = None

    output_directory.mkdir(parents=True, exist_ok=True)
    write_document(current, output_directory / "availability.json")
    if not options.dry_run and availability_changed:
        # Persist fresh scrape results before any notification attempt.
        write_document(current, data_path)
        availability_written = True

    if options.dry_run:
        next_state = updated_notification_state(
            state,
            observation.target_ids,
            observation.target_scopes,
            "dry_run_preview",
            checked_at,
            initialized_facility_ids=observation.target_facility_ids,
        )
        notification_status = "dry_run"
    elif options.test_notification:
        line_result = send_line_test_notification(token, user_id, opener)
        notification_status = (
            "test_notification_succeeded"
            if line_result.succeeded
            else f"test_notification_{line_result.status}"
        )
        next_state = updated_notification_state(
            state,
            set(state.get("observed_slot_ids", [])),
            dict(state.get("observed_slot_scopes", {})),
            notification_status,
            checked_at,
            initialized=bool(state.get("initialized", False)),
            initialized_facility_ids=state_initialized_facility_ids(
                state, previous
            ),
        )
    elif options.initialize_notification_baseline or not state.get("initialized", False):
        notification_status = "baseline_initialized"
        next_state = updated_notification_state(
            state,
            observation.target_ids,
            observation.target_scopes,
            notification_status,
            checked_at,
            initialized_facility_ids=observation.target_facility_ids,
        )
    elif not options.send_notification:
        notification_status = "notification_suppressed_baseline_advanced"
        next_state = updated_notification_state(
            state,
            observation.target_ids,
            observation.target_scopes,
            notification_status,
            checked_at,
            initialized_facility_ids=observation.target_facility_ids,
        )
    elif not observation.candidates:
        notification_status = "no_new_slots"
        next_state = updated_notification_state(
            state,
            observation.target_ids,
            observation.target_scopes,
            notification_status,
            checked_at,
            initialized_facility_ids=observation.target_facility_ids,
        )
    else:
        line_result = send_line_notification_result(
            observation.candidates,
            token=token,
            user_id=user_id,
            opener=opener,
        )
        if line_result.succeeded:
            notification_status = "notification_succeeded"
            next_state = updated_notification_state(
                state,
                observation.target_ids,
                observation.target_scopes,
                notification_status,
                checked_at,
                initialized_facility_ids=observation.target_facility_ids,
            )
        else:
            notification_status = f"notification_{line_result.status}"
            next_state = updated_notification_state(
                state,
                observation.failure_ids,
                observation.failure_scopes,
                notification_status,
                checked_at,
                initialized_facility_ids=observation.failure_facility_ids,
            )
            print(
                f"::warning::LINE notification failed ({line_result.status}); "
                "notification baseline was not advanced."
            )

    if not options.dry_run:
        if next_state != state:
            write_notification_state(next_state, state_path)
            state_written = True

    write_notification_state(next_state, output_directory / "notification-state.json")
    print(
        f"new_slots={len(observation.candidates)} "
        f"notification={notification_status} dry_run={options.dry_run}"
    )
    return RunResult(
        availability_written=availability_written,
        notification_state_written=state_written,
        notification_status=notification_status,
        notification_candidates=len(observation.candidates),
        line_result=line_result,
    )


def environment_boolean(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def run_options_from_environment() -> RunOptions:
    return RunOptions(
        dry_run=environment_boolean("DRY_RUN"),
        send_notification=environment_boolean("SEND_NOTIFICATION"),
        test_notification=environment_boolean("TEST_NOTIFICATION"),
        initialize_notification_baseline=environment_boolean(
            "INITIALIZE_NOTIFICATION_BASELINE"
        ),
    )


def main() -> int:
    previous = load_document()
    state = load_notification_state()
    targets = generate_target_days()
    current = build_document(targets)
    process_scrape_result(
        previous,
        current,
        state,
        run_options_from_environment(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
