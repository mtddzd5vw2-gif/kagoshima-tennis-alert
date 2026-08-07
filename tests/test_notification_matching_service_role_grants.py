from __future__ import annotations

import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
MIGRATION_DIR = ROOT / "supabase/migrations"
GRANT_MIGRATION_PATH = (
    MIGRATION_DIR
    / "20260807120000_grant_notification_matching_rpc_dependencies.sql"
)
MATCHING_RPC_MIGRATION_PATH = (
    MIGRATION_DIR / "20260807110000_add_notification_rule_matching_rpc.sql"
)
MATCHING_RPC_MIGRATION_HASH = (
    "78960506013095857a4119dd44d89bdf38ff6b37ab452c8cbadbb352b9cd5e48"
)
EXPECTED_TABLES = {
    "profiles",
    "notification_rules",
    "notification_rule_facilities",
    "notification_rule_weekdays",
}


def compact(value: str) -> str:
    without_comments = re.sub(r"--.*?$", "", value, flags=re.MULTILINE)
    return " ".join(without_comments.split())


def grant_migration_sql() -> str:
    return compact(GRANT_MIGRATION_PATH.read_text(encoding="utf-8").lower())


def test_service_role_dependency_grant_migration_exists_after_rpc() -> None:
    assert GRANT_MIGRATION_PATH.is_file()
    assert MATCHING_RPC_MIGRATION_PATH.name < GRANT_MIGRATION_PATH.name


def test_service_role_receives_select_on_exactly_the_rpc_dependencies() -> None:
    sql = grant_migration_sql()
    grant = re.fullmatch(
        r"grant select on table (?P<tables>.*?) to service_role;",
        sql,
    )

    assert grant
    granted_tables = set(
        re.findall(r"\bpublic\.([a-z_]+)\b", grant.group("tables"))
    )
    assert granted_tables == EXPECTED_TABLES
    assert len(re.findall(r"\bpublic\.[a-z_]+\b", grant.group("tables"))) == 4


def test_dependency_grant_does_not_target_browser_or_public_roles() -> None:
    sql = grant_migration_sql()

    assert not re.search(r"\bto\s+(?:anon|authenticated|public)\b", sql)
    assert re.findall(r"\bto\s+([a-z_]+)\s*;", sql) == ["service_role"]


def test_dependency_grant_has_no_write_or_ownership_privileges() -> None:
    sql = grant_migration_sql()

    for forbidden_privilege in (
        "insert",
        "update",
        "delete",
        "truncate",
        "references",
        "trigger",
    ):
        assert not re.search(rf"\b{forbidden_privilege}\b", sql)
    assert len(re.findall(r"\bgrant\b", sql)) == 1


def test_dependency_grant_does_not_include_other_tables_or_functions() -> None:
    sql = grant_migration_sql()

    assert "auth.users" not in sql
    assert set(re.findall(r"\bpublic\.([a-z_]+)\b", sql)) == EXPECTED_TABLES
    assert not re.search(r"\b(?:create|alter|replace)\s+function\b", sql)


def test_existing_matching_rpc_migration_is_unchanged_and_invoker() -> None:
    normalized_bytes = MATCHING_RPC_MIGRATION_PATH.read_bytes().replace(
        b"\r\n", b"\n"
    )
    assert hashlib.sha256(normalized_bytes).hexdigest() == (
        MATCHING_RPC_MIGRATION_HASH
    )

    rpc_sql = compact(
        MATCHING_RPC_MIGRATION_PATH.read_text(encoding="utf-8").lower()
    )
    assert "security invoker" in rpc_sql
    assert "security definer" not in rpc_sql
