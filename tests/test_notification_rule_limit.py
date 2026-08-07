from __future__ import annotations

import re
import threading
from collections.abc import Callable
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
MIGRATION_PATH = (
    ROOT
    / "supabase/migrations"
    / "20260807130000_limit_notification_rules_per_user.sql"
)
MAX_NOTIFICATION_RULES = 5
LIMIT_ERROR = "Notification rule limit of 5 has been reached."


def read_migration() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8").lower()


def compact(value: str) -> str:
    without_comments = re.sub(r"--.*?$", "", value, flags=re.MULTILINE)
    return " ".join(without_comments.split())


def trigger_function(sql: str) -> str:
    match = re.search(
        r"create\s+function\s+public\.enforce_notification_rule_limit\(\)"
        r"(.*?)\$\$;",
        sql,
        re.DOTALL,
    )
    assert match
    return compact(match.group(1))


class NotificationRuleStore:
    def __init__(self) -> None:
        self.rules: dict[str, dict[str, object]] = {}
        self.limit_checks = 0
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _user_lock(self, user_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(user_id, threading.Lock())

    def count(self, user_id: str) -> int:
        return sum(rule["user_id"] == user_id for rule in self.rules.values())

    def insert(
        self,
        rule_id: str,
        user_id: str,
        *,
        is_enabled: bool = False,
        after_lock: Callable[[], None] | None = None,
    ) -> None:
        with self._user_lock(user_id):
            if after_lock:
                after_lock()
            self.limit_checks += 1
            if self.count(user_id) >= MAX_NOTIFICATION_RULES:
                raise ValueError(LIMIT_ERROR)
            self.rules[rule_id] = {
                "user_id": user_id,
                "is_enabled": is_enabled,
                "name": rule_id,
            }

    def update(
        self,
        rule_id: str,
        *,
        user_id: str | None = None,
        name: str | None = None,
        is_enabled: bool | None = None,
    ) -> None:
        rule = self.rules[rule_id]
        current_user_id = str(rule["user_id"])
        target_user_id = current_user_id if user_id is None else user_id

        if target_user_id != current_user_id:
            with self._user_lock(target_user_id):
                self.limit_checks += 1
                target_count = sum(
                    existing_id != rule_id
                    and existing_rule["user_id"] == target_user_id
                    for existing_id, existing_rule in self.rules.items()
                )
                if target_count >= MAX_NOTIFICATION_RULES:
                    raise ValueError(LIMIT_ERROR)
                rule["user_id"] = target_user_id

        if name is not None:
            rule["name"] = name
        if is_enabled is not None:
            rule["is_enabled"] = is_enabled

    def delete(self, rule_id: str) -> None:
        del self.rules[rule_id]


def fill_rules(
    store: NotificationRuleStore,
    user_id: str,
    count: int,
    *,
    is_enabled: bool = False,
) -> None:
    for index in range(count):
        store.insert(
            f"{user_id}-{index}",
            user_id,
            is_enabled=is_enabled,
        )


def test_limit_migration_follows_the_applied_save_rpc_migration() -> None:
    assert MIGRATION_PATH.is_file()
    assert MIGRATION_PATH.name == (
        "20260807130000_limit_notification_rules_per_user.sql"
    )
    assert (
        "20260807100000_add_notification_rule_save_rpc.sql"
        < MIGRATION_PATH.name
    )


def test_migration_runs_inside_an_explicit_transaction() -> None:
    sql = compact(read_migration())
    begin_position = sql.index("begin;")
    lock_position = sql.index(
        "lock table public.notification_rules in share row exclusive mode"
    )
    commit_position = sql.rindex("commit;")

    assert sql.startswith("begin;")
    assert begin_position < lock_position < commit_position
    assert sql.endswith("commit;")


def test_migration_precheck_rejects_existing_users_above_five_anonymously() -> None:
    sql = compact(read_migration())
    lock_position = sql.index(
        "lock table public.notification_rules in share row exclusive mode"
    )
    precheck_position = sql.index("do language plpgsql")
    function_position = sql.index(
        "create function public.enforce_notification_rule_limit()"
    )
    precheck = sql[precheck_position:function_position]

    assert lock_position < precheck_position
    assert "v_max_notification_rules constant pg_catalog.int4 := 5" in precheck
    assert "group by notification_rule.user_id" in precheck
    assert "having pg_catalog.count(*) > v_max_notification_rules" in precheck
    assert "existing data exceeds 5 rules per user" in precheck
    error_message = re.search(r"raise exception\s+'([^']+)'", precheck)
    assert error_message
    assert "user_id" not in error_message.group(1)
    assert "email" not in error_message.group(1)


def test_limit_function_is_security_invoker_with_an_empty_search_path() -> None:
    sql = read_migration()
    function = trigger_function(sql)

    assert "returns trigger" in function
    assert "language plpgsql" in function
    assert "security invoker" in function
    assert "set search_path = ''" in function
    assert "security definer" not in function
    assert "v_max_notification_rules constant pg_catalog.int4 := 5" in function
    assert "disable row level security" not in sql
    assert "create policy" not in sql
    assert "alter policy" not in sql
    assert "enable row level security" not in sql
    assert "disable row level security" not in sql


def test_advisory_transaction_lock_is_stable_and_precedes_both_counts() -> None:
    function = trigger_function(read_migration())
    lock_position = function.index("pg_catalog.pg_advisory_xact_lock")
    count_positions = [
        match.start()
        for match in re.finditer(r"select pg_catalog\.count\(\*\)", function)
    ]

    assert (
        "pg_catalog.hashtextextended( "
        "new.user_id::pg_catalog.text, 0::pg_catalog.int8 )"
    ) in function
    assert len(count_positions) == 2
    assert all(lock_position < position for position in count_positions)
    assert "where notification_rule.user_id = new.user_id" in function
    assert "notification_rule.id <> old.id" in function


def test_trigger_covers_insert_and_only_user_id_updates() -> None:
    sql = compact(read_migration())

    assert (
        "create trigger enforce_notification_rules_per_user_limit "
        "before insert or update of user_id on public.notification_rules "
        "for each row execute function "
        "public.enforce_notification_rule_limit();"
    ) in sql
    assert (
        "if tg_op = 'update' and new.user_id is not distinct from old.user_id "
        "then return new; end if;"
    ) in trigger_function(read_migration())
    assert LIMIT_ERROR.lower() in sql


def test_trigger_function_has_no_direct_browser_execute_permission() -> None:
    sql = compact(read_migration())
    grants = re.findall(r"\bgrant\b.*?;", sql)

    assert not grants
    assert (
        "revoke all on function public.enforce_notification_rule_limit() "
        "from public, anon, authenticated;"
    ) in sql
    assert "comment on function public.enforce_notification_rule_limit()" in sql
    assert (
        "comment on trigger enforce_notification_rules_per_user_limit "
        "on public.notification_rules"
    ) in sql


def test_zero_through_four_existing_rules_allow_an_insert() -> None:
    for existing_count in range(MAX_NOTIFICATION_RULES):
        store = NotificationRuleStore()
        fill_rules(store, "user-a", existing_count)

        store.insert("new-rule", "user-a")

        assert store.count("user-a") == existing_count + 1


def test_sixth_rule_is_rejected_and_paused_rules_count() -> None:
    store = NotificationRuleStore()
    fill_rules(store, "user-a", MAX_NOTIFICATION_RULES, is_enabled=False)

    with pytest.raises(ValueError, match=re.escape(LIMIT_ERROR)):
        store.insert("sixth-rule", "user-a", is_enabled=True)

    assert store.count("user-a") == MAX_NOTIFICATION_RULES


def test_edit_toggle_and_delete_then_add_work_at_the_limit() -> None:
    store = NotificationRuleStore()
    fill_rules(store, "user-a", MAX_NOTIFICATION_RULES)
    checks_before_edit = store.limit_checks

    store.update("user-a-0", name="edited", is_enabled=True)

    assert store.rules["user-a-0"]["name"] == "edited"
    assert store.rules["user-a-0"]["is_enabled"] is True
    assert store.limit_checks == checks_before_edit

    store.delete("user-a-4")
    store.insert("replacement", "user-a")

    assert store.count("user-a") == MAX_NOTIFICATION_RULES
    assert "replacement" in store.rules


def test_different_users_are_counted_independently() -> None:
    store = NotificationRuleStore()
    fill_rules(store, "user-a", MAX_NOTIFICATION_RULES)
    fill_rules(store, "user-b", MAX_NOTIFICATION_RULES - 1)

    store.insert("user-b-last", "user-b")

    assert store.count("user-a") == MAX_NOTIFICATION_RULES
    assert store.count("user-b") == MAX_NOTIFICATION_RULES


def test_owner_change_checks_destination_and_excludes_the_updated_rule() -> None:
    store = NotificationRuleStore()
    fill_rules(store, "user-a", 1)
    fill_rules(store, "user-b", MAX_NOTIFICATION_RULES)

    with pytest.raises(ValueError, match=re.escape(LIMIT_ERROR)):
        store.update("user-a-0", user_id="user-b")

    store.delete("user-b-4")
    store.update("user-a-0", user_id="user-b")

    assert store.count("user-a") == 0
    assert store.count("user-b") == MAX_NOTIFICATION_RULES


def test_concurrent_same_user_inserts_are_serialized_at_five() -> None:
    store = NotificationRuleStore()
    fill_rules(store, "user-a", MAX_NOTIFICATION_RULES - 1)
    start = threading.Barrier(3)
    outcomes: list[str] = []
    outcomes_guard = threading.Lock()

    def insert(rule_id: str) -> None:
        start.wait()
        try:
            store.insert(rule_id, "user-a")
            outcome = "created"
        except ValueError:
            outcome = "rejected"
        with outcomes_guard:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=insert, args=(f"concurrent-{index}",))
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["created", "rejected"]
    assert store.count("user-a") == MAX_NOTIFICATION_RULES
