import json
from pathlib import Path

import pytest

from scripts import scrape


TARGET_DATE = "2026-08-01"
CHECKED_AT = "2026-07-21T16:00:00+09:00"


def make_slot(
    slot_id: str,
    facility_id: str = "kamoike-prefectural",
    facility_name: str = "鴨池県営テニスコート",
    court_name: str = "コート2",
    start_time: str = "11:00",
    end_time: str = "13:00",
    reservation_url: str = "https://example.test/reserve",
) -> dict:
    return {
        "facility_id": facility_id,
        "facility_name": facility_name,
        "date": TARGET_DATE,
        "court_name": court_name,
        "start_time": start_time,
        "end_time": end_time,
        "duration_minutes": 120,
        "status": "available",
        "reservation_url": reservation_url,
        "slot_id": slot_id,
    }


def make_document(
    slots: list[dict],
    statuses: dict[str, str] | None = None,
) -> dict:
    status_map = statuses or {
        str(slot["facility_id"]): "success" for slot in slots
    }
    facilities = []
    for facility_id, status in status_map.items():
        facility_slots = [
            slot for slot in slots if slot["facility_id"] == facility_id
        ]
        facilities.append(
            {
                "id": facility_id,
                "name": facility_slots[0]["facility_name"] if facility_slots else facility_id,
                "dates": [
                    {
                        "date": TARGET_DATE,
                        "day_type": "weekend",
                        "holiday_name": None,
                        "status": status,
                        "error_type": "navigation_timeout" if status == "error" else None,
                        "error_message": "timed out" if status == "error" else None,
                        "checked_at": CHECKED_AT,
                        "reservation_url": "https://example.test/reserve",
                        "availability": facility_slots if status == "success" else [],
                    }
                ],
            }
        )
    document = scrape.empty_document()
    document["generated_at"] = CHECKED_AT
    document["facilities"] = facilities
    return document


def make_state(
    slot_ids: list[str],
    initialized: bool = True,
    initialized_facility_ids: list[str] | None = None,
) -> dict:
    return {
        "schema_version": 2,
        "initialized": initialized,
        "initialized_facility_ids": (
            sorted(initialized_facility_ids)
            if initialized_facility_ids is not None
            else (
                ["kamoike-prefectural", "sumizei"]
                if initialized
                else []
            )
        ),
        "updated_at": CHECKED_AT if initialized else None,
        "observed_slot_ids": sorted(slot_ids),
        "observed_slot_scopes": {
            slot_id: f"kamoike-prefectural|{TARGET_DATE}" for slot_id in slot_ids
        },
        "last_notification_status": "baseline_initialized" if initialized else None,
    }


class FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None


class RecordingOpener:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.payloads: list[dict] = []
        self.timeouts: list[int] = []

    def __call__(self, request, timeout: int) -> FakeResponse:
        self.payloads.append(json.loads(request.data.decode("utf-8")))
        self.timeouts.append(timeout)
        return FakeResponse(self.status)


def run_process(
    tmp_path: Path,
    previous: dict,
    current: dict,
    state: dict,
    options: scrape.RunOptions,
    opener=None,
    token: str | None = "token",
    user_id: str | None = "user",
) -> tuple[scrape.RunResult, Path, Path]:
    data_path = tmp_path / "data" / "availability.json"
    state_path = tmp_path / "data" / "notification-state.json"
    result = scrape.process_scrape_result(
        previous,
        current,
        state,
        options,
        data_path=data_path,
        state_path=state_path,
        output_directory=tmp_path / "run-output",
        token=token,
        user_id=user_id,
        opener=opener,
    )
    return result, data_path, state_path


def test_missing_notification_state_requires_baseline(tmp_path: Path) -> None:
    state = scrape.load_notification_state(tmp_path / "missing.json")

    assert state["initialized"] is False
    assert state["observed_slot_ids"] == []


def test_corrupt_notification_state_requires_baseline(tmp_path: Path) -> None:
    path = tmp_path / "notification-state.json"
    path.write_text("{broken", encoding="utf-8")

    state = scrape.load_notification_state(path)

    assert state["initialized"] is False
    assert state["observed_slot_ids"] == []


def test_initialized_false_only_initializes_without_notification(tmp_path: Path) -> None:
    slot = make_slot("slot-a")
    opener = RecordingOpener()
    result, _, state_path = run_process(
        tmp_path,
        make_document([]),
        make_document([slot]),
        make_state([], initialized=False),
        scrape.RunOptions(send_notification=True),
        opener,
    )

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert result.notification_status == "baseline_initialized"
    assert saved["initialized"] is True
    assert saved["observed_slot_ids"] == ["slot-a"]
    assert opener.payloads == []


def test_seeded_baseline_matches_current_slots() -> None:
    availability = scrape.load_document()
    state = scrape.load_notification_state()
    current_ids = set(scrape.available_slot_keys(availability))

    assert current_ids
    assert set(state["observed_slot_ids"]) == current_ids


def test_first_baseline_does_not_notify_current_slots(tmp_path: Path) -> None:
    current = scrape.load_document()
    current_ids = set(scrape.available_slot_keys(current))
    opener = RecordingOpener()
    assert current_ids

    result, _, state_path = run_process(
        tmp_path,
        scrape.empty_document(),
        current,
        scrape.empty_notification_state(),
        scrape.RunOptions(send_notification=True),
        opener,
    )

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert result.notification_status == "baseline_initialized"
    assert set(saved["observed_slot_ids"]) == current_ids
    assert opener.payloads == []


def test_new_slot_id_only_is_candidate() -> None:
    old_slot = make_slot("slot-old")
    new_slot = make_slot("slot-new", court_name="コート3")
    observation = scrape.observe_notification_changes(
        make_state(["slot-old"]),
        make_document([old_slot]),
        make_document([old_slot, new_slot]),
    )

    assert [slot["slot_id"] for slot in observation.candidates] == ["slot-new"]


def test_p_kashikan_corrected_ids_migrate_without_duplicate_notifications(
    tmp_path: Path,
) -> None:
    definitions = [
        (
            "sumizei",
            "SuMIzeiテニスコート",
            "テニスコート2",
            "10:59",
            "11:59",
            "11:00",
            "12:00",
        ),
        (
            "toukai-tennis",
            "東開庭球場",
            "C・Dコート(ナイターあり)",
            "11:59",
            "12:59",
            "12:00",
            "13:00",
        ),
    ]
    previous_slots = []
    current_slots = []
    for (
        facility_id,
        facility_name,
        court_name,
        old_start,
        old_end,
        new_start,
        new_end,
    ) in definitions:
        previous_slots.append(
            make_slot(
                scrape.make_slot_id(
                    facility_id, TARGET_DATE, court_name, old_start, old_end
                ),
                facility_id=facility_id,
                facility_name=facility_name,
                court_name=court_name,
                start_time=old_start,
                end_time=old_end,
            )
        )
        current_slots.append(
            make_slot(
                scrape.make_slot_id(
                    facility_id, TARGET_DATE, court_name, new_start, new_end
                ),
                facility_id=facility_id,
                facility_name=facility_name,
                court_name=court_name,
                start_time=new_start,
                end_time=new_end,
            )
        )
    kamoike = make_slot("kamoike-existing")
    previous_slots.append(kamoike)
    current_slots.append(kamoike)
    statuses = {
        "kamoike-prefectural": "success",
        "sumizei": "success",
        "toukai-tennis": "success",
    }
    state = make_state(
        [slot["slot_id"] for slot in previous_slots],
        initialized_facility_ids=list(statuses),
    )
    state["observed_slot_scopes"] = {
        slot["slot_id"]: f"{slot['facility_id']}|{TARGET_DATE}"
        for slot in previous_slots
    }
    opener = RecordingOpener()

    result, _, state_path = run_process(
        tmp_path,
        make_document(previous_slots, statuses),
        make_document(current_slots, statuses),
        state,
        scrape.RunOptions(send_notification=True),
        opener,
    )

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert result.notification_status == "no_new_slots"
    assert result.notification_candidates == 0
    assert set(saved["observed_slot_ids"]) == {
        slot["slot_id"] for slot in current_slots
    }
    assert "kamoike-existing" in saved["observed_slot_ids"]
    assert opener.payloads == []


def test_toukai_first_observation_is_baselined_without_notification(
    tmp_path: Path,
) -> None:
    toukai_slot = make_slot(
        "toukai-existing",
        facility_id="toukai-tennis",
        facility_name="東開庭球場",
        court_name="Aコート(ナイターあり)",
        start_time="08:30",
        end_time="10:00",
    )
    statuses = {
        "kamoike-prefectural": "success",
        "sumizei": "success",
        "toukai-tennis": "success",
    }
    opener = RecordingOpener()

    result, _, state_path = run_process(
        tmp_path,
        make_document([], {"kamoike-prefectural": "success", "sumizei": "success"}),
        make_document([toukai_slot], statuses),
        make_state([]),
        scrape.RunOptions(send_notification=True),
        opener,
    )

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert result.notification_status == "no_new_slots"
    assert saved["observed_slot_ids"] == ["toukai-existing"]
    assert saved["initialized_facility_ids"] == [
        "kamoike-prefectural",
        "sumizei",
        "toukai-tennis",
    ]
    assert opener.payloads == []


def test_toukai_baseline_does_not_suppress_existing_facility_candidate(
    tmp_path: Path,
) -> None:
    existing_facility_slot = make_slot("kamoike-new")
    toukai_slot = make_slot(
        "toukai-existing",
        facility_id="toukai-tennis",
        facility_name="東開庭球場",
        court_name="Bコート(ナイターなし)",
    )
    statuses = {
        "kamoike-prefectural": "success",
        "sumizei": "success",
        "toukai-tennis": "success",
    }
    opener = RecordingOpener()

    result, _, state_path = run_process(
        tmp_path,
        make_document([], {"kamoike-prefectural": "success", "sumizei": "success"}),
        make_document([existing_facility_slot, toukai_slot], statuses),
        make_state([]),
        scrape.RunOptions(send_notification=True),
        opener,
    )

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert result.notification_status == "notification_succeeded"
    assert result.notification_candidates == 1
    assert set(saved["observed_slot_ids"]) == {"kamoike-new", "toukai-existing"}
    assert len(opener.payloads) == 1
    message = opener.payloads[0]["messages"][0]["text"]
    assert "鴨池県営テニスコート" in message
    assert "東開庭球場" not in message


def test_line_failure_keeps_existing_candidate_but_baselines_toukai(
    tmp_path: Path,
) -> None:
    existing_facility_slot = make_slot("kamoike-new")
    toukai_slot = make_slot(
        "toukai-existing",
        facility_id="toukai-tennis",
        facility_name="東開庭球場",
    )
    statuses = {
        "kamoike-prefectural": "success",
        "sumizei": "success",
        "toukai-tennis": "success",
    }
    current = make_document([existing_facility_slot, toukai_slot], statuses)
    previous = make_document(
        [], {"kamoike-prefectural": "success", "sumizei": "success"}
    )

    result, _, state_path = run_process(
        tmp_path,
        previous,
        current,
        make_state([]),
        scrape.RunOptions(send_notification=True),
        RecordingOpener(status=500),
    )

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert result.notification_status == "notification_http_error"
    assert saved["observed_slot_ids"] == ["toukai-existing"]
    retry = scrape.observe_notification_changes(saved, current, current)
    assert [slot["slot_id"] for slot in retry.candidates] == ["kamoike-new"]
    assert retry.suppressed_initial_ids == set()


def test_disappeared_slot_is_not_notified() -> None:
    slot = make_slot("slot-old")
    observation = scrape.observe_notification_changes(
        make_state(["slot-old"]), make_document([slot]), make_document([])
    )

    assert observation.candidates == []
    assert observation.target_ids == set()


def test_identical_document_has_no_notification_candidate() -> None:
    slot = make_slot("slot-old")
    document = make_document([slot])

    observation = scrape.observe_notification_changes(
        make_state(["slot-old"]), document, document
    )

    assert observation.candidates == []


def test_disappeared_slot_is_notified_if_it_reappears_after_state_advance() -> None:
    slot = make_slot("slot-a")
    disappeared = scrape.observe_notification_changes(
        make_state(["slot-a"]), make_document([slot]), make_document([])
    )
    advanced = scrape.updated_notification_state(
        make_state(["slot-a"]),
        disappeared.target_ids,
        disappeared.target_scopes,
        "no_new_slots",
        CHECKED_AT,
    )
    reappeared = scrape.observe_notification_changes(
        advanced, make_document([]), make_document([slot])
    )

    assert [candidate["slot_id"] for candidate in reappeared.candidates] == ["slot-a"]


def test_line_success_advances_notification_baseline(tmp_path: Path) -> None:
    slot = make_slot("slot-new")
    result, _, state_path = run_process(
        tmp_path,
        make_document([]),
        make_document([slot]),
        make_state([]),
        scrape.RunOptions(send_notification=True),
        RecordingOpener(),
    )

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert result.line_result and result.line_result.succeeded
    assert saved["observed_slot_ids"] == ["slot-new"]


def test_line_failure_does_not_advance_notification_baseline(tmp_path: Path) -> None:
    slot = make_slot("slot-new")
    result, _, state_path = run_process(
        tmp_path,
        make_document([]),
        make_document([slot]),
        make_state([]),
        scrape.RunOptions(send_notification=True),
        RecordingOpener(status=500),
    )

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert result.line_result and not result.line_result.succeeded
    assert saved["observed_slot_ids"] == []


def test_line_failure_still_writes_availability(tmp_path: Path) -> None:
    slot = make_slot("slot-new")
    _, data_path, _ = run_process(
        tmp_path,
        make_document([]),
        make_document([slot]),
        make_state([]),
        scrape.RunOptions(send_notification=True),
        RecordingOpener(status=500),
    )

    saved = json.loads(data_path.read_text(encoding="utf-8"))
    assert "slot-new" in scrape.available_slot_keys(saved)


def test_missing_secrets_skips_safely_and_retains_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_USER_ID", raising=False)
    slot = make_slot("slot-new")
    result, _, state_path = run_process(
        tmp_path,
        make_document([]),
        make_document([slot]),
        make_state([]),
        scrape.RunOptions(send_notification=True),
        token=None,
        user_id=None,
    )

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert result.line_result and result.line_result.status == "missing_credentials"
    assert saved["observed_slot_ids"] == []


def test_dry_run_does_not_send_or_modify_repository_files(tmp_path: Path) -> None:
    slot = make_slot("slot-new")
    data_path = tmp_path / "data.json"
    state_path = tmp_path / "state.json"
    data_path.write_text("previous-data", encoding="utf-8")
    state_path.write_text("previous-state", encoding="utf-8")
    opener = RecordingOpener()

    result = scrape.process_scrape_result(
        make_document([]),
        make_document([slot]),
        make_state([]),
        scrape.RunOptions(dry_run=True, send_notification=True, test_notification=True),
        data_path=data_path,
        state_path=state_path,
        output_directory=tmp_path / "run-output",
        token="token",
        user_id="user",
        opener=opener,
    )

    assert result.notification_status == "dry_run"
    assert data_path.read_text(encoding="utf-8") == "previous-data"
    assert state_path.read_text(encoding="utf-8") == "previous-state"
    assert opener.payloads == []


def test_send_notification_false_advances_baseline_without_sending(
    tmp_path: Path,
) -> None:
    slot = make_slot("slot-new")
    opener = RecordingOpener()
    result, _, state_path = run_process(
        tmp_path,
        make_document([]),
        make_document([slot]),
        make_state([]),
        scrape.RunOptions(send_notification=False),
        opener,
    )

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert result.notification_status == "notification_suppressed_baseline_advanced"
    assert saved["observed_slot_ids"] == ["slot-new"]
    assert opener.payloads == []


def test_initialize_baseline_never_sends(tmp_path: Path) -> None:
    slot = make_slot("slot-new")
    opener = RecordingOpener()
    result, _, state_path = run_process(
        tmp_path,
        make_document([]),
        make_document([slot]),
        make_state([]),
        scrape.RunOptions(
            send_notification=True, initialize_notification_baseline=True
        ),
        opener,
    )

    assert result.notification_status == "baseline_initialized"
    assert json.loads(state_path.read_text(encoding="utf-8"))[
        "observed_slot_ids"
    ] == ["slot-new"]
    assert opener.payloads == []


def test_test_notification_uses_one_fixed_safe_message(tmp_path: Path) -> None:
    opener = RecordingOpener()
    result, _, _ = run_process(
        tmp_path,
        make_document([]),
        make_document([]),
        make_state([]),
        scrape.RunOptions(test_notification=True),
        opener,
    )

    assert result.line_result and result.line_result.succeeded
    assert len(opener.payloads) == 1
    assert opener.payloads[0]["messages"] == [
        {"type": "text", "text": scrape.LINE_TEST_MESSAGE}
    ]


def test_message_contains_japanese_weekday_and_full_reservation_url() -> None:
    slot = make_slot("slot-a", reservation_url="https://example.test/full/reservation")

    message = scrape.build_line_messages([slot])[0]

    assert "8月1日（土）" in message
    assert "https://example.test/full/reservation" in message


def test_line_groups_toukai_slots_for_the_same_date_in_one_message() -> None:
    first = make_slot(
        "toukai-a",
        facility_id="toukai-tennis",
        facility_name="東開庭球場",
        court_name="Aコート(ナイターあり)",
        start_time="08:30",
        end_time="10:00",
    )
    second = make_slot(
        "toukai-b",
        facility_id="toukai-tennis",
        facility_name="東開庭球場",
        court_name="Bコート(ナイターなし)",
        start_time="12:00",
        end_time="13:00",
    )

    messages = scrape.build_line_messages([first, second])

    assert len(messages) == 1
    assert messages[0].count("東開庭球場") == 1
    assert "Aコート(ナイターあり)" in messages[0]
    assert "Bコート(ナイターなし)" in messages[0]


def test_p_kashikan_line_messages_use_official_display_times() -> None:
    sumizei = make_slot(
        "sumizei-corrected",
        facility_id="sumizei",
        facility_name="SuMIzeiテニスコート",
        court_name="テニスコート2",
        start_time="11:00",
        end_time="12:00",
    )
    toukai = make_slot(
        "toukai-corrected",
        facility_id="toukai-tennis",
        facility_name="東開庭球場",
        court_name="C・Dコート(ナイターあり)",
        start_time="12:00",
        end_time="13:00",
    )

    message = "\n".join(scrape.build_line_messages([sumizei, toukai]))

    assert "SuMIzeiテニスコート" in message
    assert "11:00〜12:00" in message
    assert "東開庭球場" in message
    assert "12:00〜13:00" in message
    assert ":59" not in message


def test_long_notifications_split_without_truncation() -> None:
    slots = [
        make_slot(
            f"slot-{index}",
            court_name=f"非常に長いコート名{index:03d}" * 3,
            start_time=f"{8 + index % 5:02d}:00",
            end_time=f"{9 + index % 5:02d}:00",
        )
        for index in range(30)
    ]

    messages = scrape.build_line_messages(slots, max_units=400)

    assert len(messages) > 1
    assert all(scrape.utf16_units(message) <= 400 for message in messages)
    assert all(f"非常に長いコート名{index:03d}" in "\n".join(messages) for index in range(30))


def test_line_api_batches_at_most_five_messages_per_request() -> None:
    opener = RecordingOpener()
    messages = [f"message-{index}" for index in range(11)]

    result = scrape.send_line_messages(
        messages, token="token", user_id="user", opener=opener
    )

    assert result.succeeded
    assert result.request_count == 3
    assert [len(payload["messages"]) for payload in opener.payloads] == [5, 5, 1]
    assert opener.timeouts == [scrape.LINE_REQUEST_TIMEOUT_SECONDS] * 3


def test_http_error_is_captured_without_raising() -> None:
    result = scrape.send_line_messages(
        ["message"], token="token", user_id="user", opener=RecordingOpener(500)
    )

    assert not result.succeeded
    assert result.status == "http_error"
    assert result.error_message == "HTTP 500"


def test_timeout_is_captured_without_raising() -> None:
    def timeout_opener(*_args, **_kwargs):
        raise TimeoutError("secret-free timeout")

    result = scrape.send_line_messages(
        ["message"], token="token", user_id="user", opener=timeout_opener
    )

    assert not result.succeeded
    assert result.status == "timeout"


def test_one_facility_error_preserves_it_and_notifies_other_facility() -> None:
    old_slot = make_slot("slot-old")
    new_slot = make_slot(
        "slot-new",
        facility_id="sumizei",
        facility_name="SuMIzeiテニスコート",
    )
    state = make_state(["slot-old"])
    previous = make_document([old_slot], {"kamoike-prefectural": "success", "sumizei": "success"})
    current = make_document(
        [new_slot], {"kamoike-prefectural": "error", "sumizei": "success"}
    )

    observation = scrape.observe_notification_changes(state, previous, current)

    assert [slot["slot_id"] for slot in observation.candidates] == ["slot-new"]
    assert "slot-old" in observation.target_ids


def test_error_recovery_slots_are_observed_without_false_notification() -> None:
    recovered_slot = make_slot("slot-recovered")
    previous = make_document([], {"kamoike-prefectural": "error"})
    current = make_document([recovered_slot], {"kamoike-prefectural": "success"})

    observation = scrape.observe_notification_changes(make_state([]), previous, current)

    assert observation.candidates == []
    assert observation.suppressed_recovery_ids == {"slot-recovered"}


def test_workflow_safely_gates_schedule_and_manual_dry_run() -> None:
    workflow = Path(".github/workflows/update-availability.yml").read_text(
        encoding="utf-8"
    )

    assert "vars.ENABLE_SCHEDULED_RUNS == 'true'" in workflow
    assert "vars.ENABLE_LINE_NOTIFICATIONS == 'true'" in workflow
    assert "default: true" in workflow
    assert "if: env.DRY_RUN != 'true'" in workflow
    assert "needs.update.outputs.deploy_pages == 'true'" in workflow
