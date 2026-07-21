import json
import urllib.request
from datetime import date
from pathlib import Path

import pytest

from scripts import scrape


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "kamoike_schedule.html"
TARGET = scrape.TargetDay(date(2026, 8, 1), "weekend", None)
RESERVATION_URL = scrape.KAMOIKE_URL_TEMPLATE.format(date="2026-08-01")
CHECKED_AT = "2026-07-21T12:00:00+09:00"


def fixture_html() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


def parsed_result(html: str | None = None) -> dict:
    return scrape.parse_kamoike_html(
        html or fixture_html(),
        TARGET,
        RESERVATION_URL,
        CHECKED_AT,
    )


def test_generate_target_days_filters_to_weekends_and_holidays() -> None:
    targets = scrape.generate_target_days(date(2026, 7, 20), days=15)

    assert [target.date.isoformat() for target in targets] == [
        "2026-07-20",
        "2026-07-25",
        "2026-07-26",
        "2026-08-01",
        "2026-08-02",
    ]
    assert targets[0].holiday_name == "海の日"


def test_generate_target_days_rejects_empty_window() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        scrape.generate_target_days(date(2026, 7, 20), days=0)


def test_extracts_one_hour_available_slot() -> None:
    availability = parsed_result()["availability"]

    assert any(
        slot["court_name"] == "コートA"
        and slot["start_time"] == "12:00"
        and slot["end_time"] == "13:00"
        and slot["duration_minutes"] == 60
        for slot in availability
    )


def test_merges_two_consecutive_hours_for_same_court() -> None:
    availability = parsed_result()["availability"]

    merged = next(
        slot
        for slot in availability
        if slot["court_name"] == "コートA" and slot["start_time"] == "08:00"
    )
    assert merged["end_time"] == "10:00"
    assert merged["duration_minutes"] == 120


def test_excludes_slots_before_eight() -> None:
    assert all(slot["start_time"] >= "08:00" for slot in parsed_result()["availability"])


def test_excludes_slots_after_thirteen() -> None:
    assert all(slot["end_time"] <= "13:00" for slot in parsed_result()["availability"])


def test_excludes_reserved_and_unavailable_cells() -> None:
    availability = parsed_result()["availability"]

    assert len(availability) == 3
    assert all(slot["status"] == "available" for slot in availability)


def test_does_not_treat_legend_as_availability() -> None:
    availability = parsed_result()["availability"]

    assert all(slot["court_name"] in {"コートA", "コートB"} for slot in availability)


def test_keeps_multiple_courts_separate() -> None:
    availability = parsed_result()["availability"]

    assert {slot["court_name"] for slot in availability} == {"コートA", "コートB"}


def test_zero_availability_is_success() -> None:
    html = fixture_html().replace("rsv--result--yes", "rsv--result--no")
    html = html.replace('area-label="予約可"', 'area-label="予約済み"')

    result = parsed_result(html)

    assert result["status"] == "success"
    assert result["availability"] == []
    assert result["error_type"] is None


def test_dom_change_raises_unexpected_dom() -> None:
    html = """
    <div class="rsv__result" data-reserve="1">
      <section class="rsv__field">
        <h3 class="rsv__result__item"><em>コートA</em></h3>
        <ul class="rsv__result__time"><li>8:00</li><li>13:00</li></ul>
      </section>
    </div>
    """

    with pytest.raises(scrape.ScrapeStructureError) as error:
        parsed_result(html)

    assert error.value.error_type == "unexpected_dom"


def test_missing_schedule_raises_no_schedule_table() -> None:
    with pytest.raises(scrape.ScrapeStructureError) as error:
        parsed_result("<html><body>maintenance</body></html>")

    assert error.value.error_type == "no_schedule_table"


def test_duplicate_court_rows_are_deduplicated() -> None:
    availability = parsed_result()["availability"]

    court_b = [slot for slot in availability if slot["court_name"] == "コートB"]
    assert len(court_b) == 1
    assert court_b[0]["start_time"] == "09:00"
    assert court_b[0]["end_time"] == "11:00"


def test_slot_id_is_stable_and_uses_required_fields() -> None:
    first = parsed_result()["availability"][0]
    second = parsed_result()["availability"][0]

    expected = scrape.make_slot_id(
        first["facility_id"],
        first["date"],
        first["court_name"],
        first["start_time"],
        first["end_time"],
    )
    assert first["slot_id"] == expected == second["slot_id"]


def test_build_document_reflects_kamoike_availability(tmp_path: Path) -> None:
    client = FakePageClient(PageCaptureFactory.success(fixture_html()))
    kamoike = scrape.configured_facilities()[0]

    document = scrape.build_document(
        [TARGET],
        facilities=[kamoike],
        client_factory=lambda: client,
    )
    output = tmp_path / "availability.json"
    scrape.write_document(document, output)
    loaded = scrape.load_document(output)

    facility = loaded["facilities"][0]
    assert loaded["schema_version"] == 2
    assert facility["id"] == "kamoike-prefectural"
    assert facility["dates"][0]["status"] == "success"
    assert len(facility["dates"][0]["availability"]) == 3
    assert client.snapshot_name == "2026-08-01"


def test_scrape_failure_records_diagnostics() -> None:
    client = FakePageClient(
        scrape.PageCapture(
            html="<html></html>",
            checked_at=CHECKED_AT,
            response_status=None,
            error_type="navigation_timeout",
            error_message="timed out",
        )
    )
    facility = scrape.configured_facilities()[0]

    result = scrape.scrape_kamoike(client, facility, TARGET)

    assert result["status"] == "error"
    assert result["error_type"] == "navigation_timeout"
    assert result["error_message"] == "timed out"
    assert result["checked_at"] == CHECKED_AT
    assert result["reservation_url"] == RESERVATION_URL
    assert result["availability"] == []


def test_detect_new_availability_uses_slot_id() -> None:
    slot = parsed_result()["availability"][0]
    previous = {"facilities": []}
    current = {
        "facilities": [
            {"dates": [{"availability": [slot]}]},
        ]
    }

    assert scrape.detect_new_availability(previous, current) == [slot]
    assert scrape.detect_new_availability(current, current) == []


def test_line_notification_sends_new_slots(monkeypatch) -> None:
    captured: dict = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def fake_urlopen(request: urllib.request.Request, timeout: int):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    slot = parsed_result()["availability"][0]

    sent = scrape.send_line_notification([slot], token="token", user_id="user")

    payload = json.loads(captured["request"].data.decode("utf-8"))
    assert sent is True
    assert captured["timeout"] == 20
    assert payload["to"] == "user"
    assert "鴨池県営テニスコート" in payload["messages"][0]["text"]


def test_comparable_document_ignores_check_timestamps() -> None:
    previous = {
        "generated_at": "2026-07-20T10:00:00+09:00",
        "facilities": [{"dates": [{"checked_at": "old", "availability": []}]}],
    }
    current = {
        "generated_at": "2026-07-21T10:00:00+09:00",
        "facilities": [{"dates": [{"checked_at": "new", "availability": []}]}],
    }

    assert scrape.comparable_document(previous) == scrape.comparable_document(current)


class PageCaptureFactory:
    @staticmethod
    def success(html: str) -> scrape.PageCapture:
        return scrape.PageCapture(
            html=html,
            checked_at=CHECKED_AT,
            response_status=200,
        )


class FakePageClient:
    def __init__(self, capture: scrape.PageCapture) -> None:
        self.capture = capture
        self.snapshot_directory: Path | None = None
        self.snapshot_name = ""

    def __enter__(self) -> "FakePageClient":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def capture_page(
        self,
        url: str,
        snapshot_directory: Path,
        snapshot_name: str,
    ) -> scrape.PageCapture:
        self.snapshot_directory = snapshot_directory
        self.snapshot_name = snapshot_name
        return self.capture

    def extract_texts(self, url: str, selector: str) -> list[str]:
        return []
