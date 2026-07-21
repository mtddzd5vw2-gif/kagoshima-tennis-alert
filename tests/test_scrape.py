import json
import urllib.request
from datetime import date
from pathlib import Path

import pytest

from scripts import scrape


def test_generate_target_days_filters_to_weekends_and_holidays() -> None:
    targets = scrape.generate_target_days(date(2026, 7, 20), days=15)

    assert [target.date.isoformat() for target in targets] == [
        "2026-07-20",
        "2026-07-25",
        "2026-07-26",
        "2026-08-01",
        "2026-08-02",
    ]
    assert targets[0].day_type == "holiday"
    assert targets[0].holiday_name == "海の日"
    assert all(target.day_type == "weekend" for target in targets[1:])


def test_generate_target_days_rejects_empty_window() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        scrape.generate_target_days(date(2026, 7, 20), days=0)


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("07:00", "08:00", False),
        ("07:30", "08:30", True),
        ("08:00", "09:00", True),
        ("12:00", "14:00", True),
        ("13:00", "14:00", False),
    ],
)
def test_overlaps_monitor_window(start: str, end: str, expected: bool) -> None:
    assert scrape.overlaps_monitor_window(start, end) is expected


def test_parse_slot_texts_keeps_only_available_slots_in_window() -> None:
    slots = scrape.parse_slot_texts(
        [
            "Aコート 07:00〜08:00 予約可",
            "Aコート 08:00〜09:00 予約可",
            "Bコート 09:00〜10:00 予約済み",
            "Cコート 12：00〜14：00 ○",
            "Cコート 12：00〜14：00 ○",
        ],
        "https://example.com/reserve",
    )

    assert [(slot["start"], slot["end"]) for slot in slots] == [
        ("08:00", "09:00"),
        ("12:00", "14:00"),
    ]
    assert all(slot["status"] == "available" for slot in slots)


def test_detect_new_availability_returns_only_new_slot() -> None:
    previous = _document_with_slots(
        [{"start": "08:00", "end": "09:00", "court": "Aコート"}]
    )
    current = _document_with_slots(
        [
            {"start": "08:00", "end": "09:00", "court": "Aコート"},
            {"start": "10:00", "end": "11:00", "court": "Bコート"},
        ]
    )

    changes = scrape.detect_new_availability(previous, current)

    assert len(changes) == 1
    assert changes[0]["court"] == "Bコート"
    assert changes[0]["facility_name"] == "テスト施設"


def test_build_document_marks_unconfigured_selectors_as_pending(monkeypatch) -> None:
    monkeypatch.delenv("KAMOIKE_SLOT_SELECTOR", raising=False)
    monkeypatch.delenv("SUMIZEI_SLOT_SELECTOR", raising=False)
    monkeypatch.delenv("SUMIZEI_URL_TEMPLATE", raising=False)

    document = scrape.build_document(
        scrape.generate_target_days(date(2026, 7, 20), days=1)
    )

    assert document["schema_version"] == 1
    assert document["window"]["start"] == "08:00"
    assert [facility["id"] for facility in document["facilities"]] == [
        "kamoike",
        "sumizei",
    ]
    assert all(
        facility["dates"][0]["status"] == "selector_pending"
        for facility in document["facilities"]
    )


def test_kamoike_scraper_uses_facility_specific_selector(monkeypatch) -> None:
    client = FakePageClient(["Aコート 08:00〜09:00 予約可"])
    facility = scrape.configured_facilities()[0]
    target = scrape.TargetDay(date(2026, 7, 25), "weekend", None)
    monkeypatch.setenv("KAMOIKE_SLOT_SELECTOR", ".available-slot")

    result = scrape.scrape_kamoike(client, facility, target)

    assert result["status"] == "ok"
    assert result["slots"][0]["start"] == "08:00"
    assert client.selector == ".available-slot"
    assert "date=2026-07-25" in client.url


def test_write_and_load_document_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "availability.json"
    document = scrape.empty_document()

    scrape.write_document(document, path)

    assert scrape.load_document(path) == document
    assert json.loads(path.read_text(encoding="utf-8")) == document


def test_line_notification_is_skipped_without_changes() -> None:
    assert scrape.send_line_notification([], token="token", user_id="user") is False


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
    change = {
        "facility_id": "kamoike",
        "facility_name": "鴨池県営テニスコート",
        "date": "2026-07-25",
        "start": "08:00",
        "end": "09:00",
        "court": "Aコート",
        "booking_url": "https://example.com/reserve",
    }

    sent = scrape.send_line_notification([change], token="token", user_id="user")

    payload = json.loads(captured["request"].data.decode("utf-8"))
    assert sent is True
    assert captured["timeout"] == 20
    assert payload["to"] == "user"
    assert "鴨池県営テニスコート" in payload["messages"][0]["text"]


def test_comparable_document_ignores_generated_at() -> None:
    previous = {"generated_at": "2026-07-20T10:00:00+09:00", "facilities": []}
    current = {"generated_at": "2026-07-21T10:00:00+09:00", "facilities": []}

    assert scrape.comparable_document(previous) == scrape.comparable_document(current)


class FakePageClient:
    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.url = ""
        self.selector = ""

    def extract_texts(self, url: str, selector: str) -> list[str]:
        self.url = url
        self.selector = selector
        return self.texts


def _document_with_slots(slots: list[dict[str, str]]) -> dict:
    normalized_slots = [
        {
            **slot,
            "status": "available",
            "booking_url": "https://example.com/reserve",
        }
        for slot in slots
    ]
    return {
        "facilities": [
            {
                "id": "test",
                "name": "テスト施設",
                "dates": [
                    {
                        "date": "2026-07-25",
                        "slots": normalized_slots,
                    }
                ],
            }
        ]
    }
