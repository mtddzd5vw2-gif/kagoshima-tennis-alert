from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup


ROOT = Path(__file__).parents[1]
STATIC_PAGES = (
    Path("auth/login.html"),
    Path("auth/callback.html"),
    Path("account/index.html"),
    Path("legal/terms.html"),
    Path("legal/privacy.html"),
)
STATIC_ASSETS = (
    Path("assets/css/auth.css"),
    Path("assets/js/auth-foundation.js"),
    Path("assets/config/auth-config.example.js"),
)


def read(relative_path: Path | str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def local_target(source: Path, reference: str) -> Path | None:
    parsed = urlsplit(reference)
    if parsed.scheme or parsed.netloc or reference.startswith(("#", "mailto:", "tel:")):
        return None
    relative_path = unquote(parsed.path)
    if not relative_path:
        return None
    return (source.parent / relative_path).resolve()


def test_required_static_pages_and_assets_exist() -> None:
    for relative_path in (*STATIC_PAGES, *STATIC_ASSETS):
        path = ROOT / relative_path
        assert path.is_file(), f"Missing static foundation file: {relative_path}"
        assert path.stat().st_size > 0


def test_local_auth_config_is_ignored_but_sample_is_committable() -> None:
    ignored = (
        ".env",
        ".env.local",
        "assets/config/auth-config.js",
        "assets/config/development.local.js",
        "private-service-role.json",
        "authentication.key",
    )
    for relative_path in ignored:
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", relative_path],
            cwd=ROOT,
            check=False,
        )
        assert result.returncode == 0, f"{relative_path} should be ignored"

    sample = subprocess.run(
        ["git", "check-ignore", "--quiet", "assets/config/auth-config.example.js"],
        cwd=ROOT,
        check=False,
    )
    assert sample.returncode == 1


def test_static_page_links_and_assets_resolve_inside_repository() -> None:
    for relative_page in STATIC_PAGES:
        page = ROOT / relative_page
        soup = BeautifulSoup(page.read_text(encoding="utf-8"), "html.parser")
        references = [
            element.get(attribute)
            for element, attribute in (
                *((element, "href") for element in soup.find_all(href=True)),
                *((element, "src") for element in soup.find_all(src=True)),
            )
        ]

        assert references, f"No links found in {relative_page}"
        for reference in references:
            target = local_target(page, reference)
            if target is None:
                continue
            assert target.is_relative_to(ROOT.resolve())
            assert target.exists(), f"{relative_page}: broken link {reference}"


def test_project_markdown_local_links_resolve() -> None:
    markdown_link = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")

    for markdown_file in (ROOT / "README.md", *(ROOT / "docs").glob("*.md")):
        content = markdown_file.read_text(encoding="utf-8")
        for raw_target in markdown_link.findall(content):
            target_text = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            target = local_target(markdown_file, target_text)
            if target is None:
                continue
            assert target.is_relative_to(ROOT.resolve())
            assert target.exists(), f"{markdown_file.name}: broken link {target_text}"


def test_auth_pages_do_not_load_third_party_scripts_or_submit_forms() -> None:
    for relative_page in STATIC_PAGES:
        soup = BeautifulSoup(read(relative_page), "html.parser")
        for script in soup.find_all("script", src=True):
            assert not urlsplit(script["src"]).scheme
        for form in soup.find_all("form"):
            assert not form.get("action")

    login = BeautifulSoup(read("auth/login.html"), "html.parser")
    assert login.find("input", {"type": "email"})
    assert login.find("input", {"name": "terms-consent"})
    assert login.find("button", {"type": "submit"}).has_attr("disabled")


def test_callback_scrubs_credentials_without_processing_or_logging_them() -> None:
    callback = BeautifulSoup(read("auth/callback.html"), "html.parser")
    script = read("assets/js/auth-foundation.js")

    assert callback.body["data-page"] == "auth-callback"
    assert callback.find("meta", {"name": "referrer"})["content"] == "no-referrer"
    assert "history.replaceState" in script
    assert "console." not in script
    assert "fetch(" not in script
    assert "supabase." not in script.lower()


def test_legal_pages_are_explicitly_drafts_requiring_review() -> None:
    for relative_page in (Path("legal/terms.html"), Path("legal/privacy.html")):
        text = BeautifulSoup(read(relative_page), "html.parser").get_text(" ", strip=True)
        assert "暫定案" in text
        assert "一般公開前" in text
        assert "内容確認が必要" in text


def test_public_config_sample_has_only_expected_empty_values() -> None:
    config = read("assets/config/auth-config.example.js")
    entries = dict(
        re.findall(r"^\s{2}([A-Za-z][A-Za-z0-9]*):\s*\"([^\"]*)\",$", config, re.MULTILINE)
    )

    assert entries == {
        "supabaseUrl": "",
        "supabasePublishableKey": "",
        "authCallbackUrl": "",
    }
    assert "publishable key" in config
    assert "service role key" in config
    assert "Never put" in config


def test_static_foundation_contains_no_credential_like_values() -> None:
    files = [ROOT / path for path in (*STATIC_PAGES, *STATIC_ASSETS)]
    forbidden_value_patterns = {
        "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),
        "Supabase secret key": re.compile(r"\bsb_secret_[A-Za-z0-9_-]{10,}"),
        "Supabase service role JWT assignment": re.compile(
            r"service[_ -]?role(?:_key)?\s*[:=]\s*[\"'][^\"']{8,}",
            re.IGNORECASE,
        ),
        "real Supabase project URL": re.compile(
            r"https://[a-z0-9]{8,}\.supabase\.co", re.IGNORECASE
        ),
        "private key material": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    }

    for path in files:
        content = path.read_text(encoding="utf-8")
        for label, pattern in forbidden_value_patterns.items():
            assert not pattern.search(content), f"{label} found in {path.relative_to(ROOT)}"


def test_pages_workflow_publishes_phase_zero_and_auth_foundation() -> None:
    workflow = read(".github/workflows/update-availability.yml")

    for artifact_path in ("index.html", "auth", "account", "legal", "assets"):
        assert re.search(rf"^\s{{12}}{re.escape(artifact_path)}$", workflow, re.MULTILINE)

    assert "cp run-artifact/index.html _site/index.html" in workflow
    assert (
        "cp run-artifact/run-output/availability.json "
        "_site/data/availability.json"
    ) in workflow
    for directory in ("auth", "account", "legal", "assets"):
        assert f"cp -R run-artifact/{directory} _site/{directory}" in workflow

    assert "python scripts/scrape.py" in workflow
    assert "LINE_CHANNEL_ACCESS_TOKEN" in workflow
    assert "LINE_USER_ID" in workflow
    assert "data/notification-state.json" in workflow


def test_existing_phase_zero_page_and_public_json_contract_remain() -> None:
    index = read("index.html")
    workflow = read(".github/workflows/update-availability.yml")

    assert "data/availability.json" in index
    assert "id=\"page-utils\"" in index
    assert "data/availability.json" in workflow
    assert (ROOT / "data/availability.json").is_file()
    assert (ROOT / "data/notification-state.json").is_file()
