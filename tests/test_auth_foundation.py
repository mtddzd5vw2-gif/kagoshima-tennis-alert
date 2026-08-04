from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from bs4 import BeautifulSoup
from playwright.sync_api import Browser, Playwright, sync_playwright


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
    Path("scripts/generate_auth_config.py"),
)
SUPABASE_JS_URL = (
    "https://cdn.jsdelivr.net/npm/"
    "@supabase/supabase-js@2.106.2/dist/umd/supabase.js"
)
MOCK_AUTH_CONFIG = """window.TCW_AUTH_CONFIG = Object.freeze({
  supabaseUrl: "https://project.example.supabase.co",
  supabasePublishableKey: "sb_publishable_test_public_only",
  authCallbackUrl: "http://pages.test/project/auth/callback.html",
});
"""
MOCK_SUPABASE_SDK = """
window.supabase = {
  createClient(url, key, options) {
    window.__clientArguments = { url, key, options };
    window.__authCalls = window.__authCalls || [];
    const mock = window.__mockAuth || {};
    return {
      auth: {
        signInWithOtp(payload) {
          window.__authCalls.push({ method: "signInWithOtp", payload });
          return new Promise((resolve) => {
            window.setTimeout(
              () => resolve({ error: mock.signInError ? { name: "AuthError" } : null }),
              mock.delay || 0,
            );
          });
        },
        async exchangeCodeForSession(code) {
          window.__authCalls.push({ method: "exchangeCodeForSession" });
          window.sessionStorage.setItem("mock-exchanged-code", code);
          return { error: mock.exchangeError ? { name: "AuthError" } : null };
        },
        async getSession() {
          window.__authCalls.push({ method: "getSession" });
          return {
            data: {
              session: mock.sessionEmail
                ? { user: { email: mock.sessionEmail } }
                : null,
            },
            error: mock.sessionError ? { name: "AuthError" } : null,
          };
        },
        async signOut() {
          window.__authCalls.push({ method: "signOut" });
          window.sessionStorage.setItem("mock-signed-out", "true");
          return { error: mock.signOutError ? { name: "AuthError" } : null };
        },
      },
    };
  },
};
"""


def read(relative_path: Path | str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def playwright_runtime() -> Playwright:
    runtime = sync_playwright().start()
    yield runtime
    runtime.stop()


@pytest.fixture(scope="module")
def browser(playwright_runtime: Playwright) -> Browser:
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    instance = playwright_runtime.chromium.launch(
        headless=True,
        executable_path=executable,
    )
    yield instance
    instance.close()


@pytest.fixture
def auth_page_loader(browser: Browser):
    contexts = []

    def load(path: str, mock: dict | None = None):
        context = browser.new_context()
        contexts.append(context)
        page = context.new_page()
        messages: list[str] = []
        page.on("console", lambda message: messages.append(message.text))
        page.add_init_script(
            script=f"window.__mockAuth = {json.dumps(mock or {})};"
        )

        def route_request(route) -> None:
            parsed = urlsplit(route.request.url)
            if route.request.url == SUPABASE_JS_URL:
                route.fulfill(
                    status=200,
                    content_type="application/javascript",
                    body=MOCK_SUPABASE_SDK,
                )
                return

            relative_path = parsed.path.removeprefix("/project/")
            if relative_path in {
                "auth/login.html",
                "auth/callback.html",
                "account/index.html",
            }:
                route.fulfill(
                    status=200,
                    content_type="text/html",
                    body=read(relative_path),
                )
            elif relative_path == "assets/config/auth-config.js":
                route.fulfill(
                    status=200,
                    content_type="application/javascript",
                    body=MOCK_AUTH_CONFIG,
                )
            elif relative_path == "assets/js/auth-foundation.js":
                route.fulfill(
                    status=200,
                    content_type="application/javascript",
                    body=read(relative_path),
                )
            elif relative_path == "assets/css/auth.css":
                route.fulfill(status=200, content_type="text/css", body="")
            else:
                route.fulfill(status=404, body="not found")

        page.route("**/*", route_request)
        page.goto(f"http://pages.test/project/{path}")
        return page, messages

    yield load
    for context in contexts:
        context.close()


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
            if target == (ROOT / "assets/config/auth-config.js").resolve():
                assert subprocess.run(
                    ["git", "check-ignore", "--quiet", str(target.relative_to(ROOT))],
                    cwd=ROOT,
                    check=False,
                ).returncode == 0
                continue
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


def test_auth_pages_load_only_pinned_supabase_sdk_and_do_not_submit_forms() -> None:
    for relative_page in STATIC_PAGES:
        soup = BeautifulSoup(read(relative_page), "html.parser")
        for script in soup.find_all("script", src=True):
            if urlsplit(script["src"]).scheme:
                assert script["src"] == SUPABASE_JS_URL
        for form in soup.find_all("form"):
            assert not form.get("action")

    login = BeautifulSoup(read("auth/login.html"), "html.parser")
    assert login.find("input", {"type": "email"})
    assert login.find("input", {"name": "terms-consent"})
    assert login.find("button", {"type": "submit"}).has_attr("disabled")


def test_auth_script_uses_pkce_and_does_not_log_credentials() -> None:
    callback = BeautifulSoup(read("auth/callback.html"), "html.parser")
    script = read("assets/js/auth-foundation.js")

    assert callback.body["data-page"] == "auth-callback"
    assert callback.find("meta", {"name": "referrer"})["content"] == "no-referrer"
    assert "history.replaceState" in script
    assert 'flowType: "pkce"' in script
    assert "persistSession: true" in script
    assert "autoRefreshToken: true" in script
    assert "signInWithOtp" in script
    assert "exchangeCodeForSession" in script
    assert "getSession" in script
    assert "signOut" in script
    assert "console." not in script
    assert "fetch(" not in script


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


def run_config_generator(
    tmp_path: Path,
    overrides: dict[str, str | None] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    environment = os.environ.copy()
    environment.update(
        {
            "SUPABASE_URL": "https://project.example.supabase.co",
            "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_test_public_only",
            "AUTH_CALLBACK_URL": (
                "http://localhost:8765/auth/callback.html"
            ),
        }
    )
    for name, value in (overrides or {}).items():
        if value is None:
            environment.pop(name, None)
        else:
            environment[name] = value

    output = tmp_path / "auth-config.js"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/generate_auth_config.py"),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, output


def test_auth_config_generator_escapes_javascript_strings(tmp_path: Path) -> None:
    publishable_key = 'sb_publishable_test"</script>&\u2028tail'
    result, output = run_config_generator(
        tmp_path,
        {"SUPABASE_PUBLISHABLE_KEY": publishable_key},
    )

    assert result.returncode == 0, result.stderr
    config = output.read_text(encoding="utf-8")
    assert publishable_key not in config
    assert "</script>" not in config
    assert "\\u003c/script\\u003e\\u0026\\u2028" in config
    assert set(
        re.findall(r"^\s{2}([A-Za-z][A-Za-z0-9]*):", config, re.MULTILINE)
    ) == {"supabaseUrl", "supabasePublishableKey", "authCallbackUrl"}


@pytest.mark.parametrize("missing_name", (
    "SUPABASE_URL",
    "SUPABASE_PUBLISHABLE_KEY",
    "AUTH_CALLBACK_URL",
))
def test_auth_config_generator_fails_when_required_value_is_missing(
    tmp_path: Path,
    missing_name: str,
) -> None:
    result, output = run_config_generator(tmp_path, {missing_name: "  "})

    assert result.returncode == 1
    assert missing_name in result.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    "forbidden_key",
    (
        "sb_secret_test_not_a_real_key",
        "test-service_role-key",
    ),
)
def test_auth_config_generator_rejects_secret_key_markers(
    tmp_path: Path,
    forbidden_key: str,
) -> None:
    result, output = run_config_generator(
        tmp_path,
        {"SUPABASE_PUBLISHABLE_KEY": forbidden_key},
    )

    assert result.returncode == 1
    assert "secret/service-role" in result.stderr
    assert not output.exists()


def test_auth_config_generator_rejects_service_role_jwt(tmp_path: Path) -> None:
    def encode(value: dict[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    fake_jwt = f"{encode({'alg': 'none'})}.{encode({'role': 'service_role'})}.test"
    result, output = run_config_generator(
        tmp_path,
        {"SUPABASE_PUBLISHABLE_KEY": fake_jwt},
    )

    assert result.returncode == 1
    assert "service-role JWT" in result.stderr
    assert not output.exists()


def test_auth_config_generator_rejects_non_publishable_value(tmp_path: Path) -> None:
    result, output = run_config_generator(
        tmp_path,
        {"SUPABASE_PUBLISHABLE_KEY": "database-password-not-a-public-key"},
    )

    assert result.returncode == 1
    assert "publishable key or legacy anon JWT" in result.stderr
    assert not output.exists()


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

    for variable in (
        "vars.SUPABASE_URL",
        "vars.SUPABASE_PUBLISHABLE_KEY",
        "vars.AUTH_CALLBACK_URL",
    ):
        assert variable in workflow
    assert "python scripts/generate_auth_config.py" in workflow
    assert "--output _site/assets/config/auth-config.js" in workflow
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


def test_magic_link_submission_validates_input_prevents_duplicates_and_is_neutral(
    auth_page_loader,
) -> None:
    page, messages = auth_page_loader(
        "auth/login.html",
        {"delay": 30},
    )
    email = page.locator('input[name="email"]')
    consent = page.locator('input[name="terms-consent"]')
    submit = page.locator('button[type="submit"]')

    assert submit.is_disabled()
    email.fill("not-an-email")
    consent.check()
    assert submit.is_disabled()

    email.fill("member@example.test")
    assert submit.is_enabled()
    page.locator("form").evaluate(
        """form => {
          form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
          form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
        }"""
    )
    page.locator('[data-form-status][data-state="success"]').wait_for()

    calls = page.evaluate("window.__authCalls")
    assert calls == [
        {
            "method": "signInWithOtp",
            "payload": {
                "email": "member@example.test",
                "options": {
                    "emailRedirectTo": (
                        "http://pages.test/project/auth/callback.html"
                    )
                },
            },
        }
    ]
    status = page.locator("[data-form-status]").inner_text()
    assert "メールを確認してください" in status
    assert "member@example.test" not in status
    assert messages == []

    arguments = page.evaluate("window.__clientArguments")
    assert arguments["url"] == "https://project.example.supabase.co"
    assert arguments["key"] == "sb_publishable_test_public_only"
    assert arguments["options"]["auth"] == {
        "flowType": "pkce",
        "persistSession": True,
        "autoRefreshToken": True,
        "detectSessionInUrl": False,
    }


def test_pkce_callback_exchanges_code_scrubs_url_and_opens_account(
    auth_page_loader,
) -> None:
    page, messages = auth_page_loader(
        "auth/callback.html?code=one-time-code&unexpected=value",
        {"sessionEmail": "member@example.test"},
    )

    page.wait_for_url("http://pages.test/project/account/index.html")
    page.locator("[data-account-content]:not([hidden])").wait_for()

    assert page.url == "http://pages.test/project/account/index.html"
    assert page.evaluate(
        'window.sessionStorage.getItem("mock-exchanged-code")'
    ) == "one-time-code"
    assert page.locator("[data-account-email]").inner_text() == "member@example.test"
    assert messages == []


def test_pkce_callback_failure_scrubs_url_and_shows_login_route(
    auth_page_loader,
) -> None:
    page, messages = auth_page_loader(
        "auth/callback.html?code=expired-code#token=must-not-remain",
        {"exchangeError": True},
    )

    page.locator('[data-callback-status][data-state="error"]').wait_for()
    assert page.url == "http://pages.test/project/auth/callback.html"
    assert page.locator("[data-callback-retry]").is_visible()
    assert "expired-code" not in page.locator("body").inner_text()
    assert messages == []


def test_account_checks_session_displays_email_and_signs_out(
    auth_page_loader,
) -> None:
    page, messages = auth_page_loader(
        "account/index.html",
        {"sessionEmail": "member@example.test"},
    )
    page.locator("[data-account-content]:not([hidden])").wait_for()

    assert page.locator("[data-account-email]").inner_text() == "member@example.test"
    assert page.locator("[data-sign-out]").is_enabled()
    assert "準備中" in page.locator("main").inner_text()

    page.locator("[data-sign-out]").click()
    page.wait_for_url("http://pages.test/project/auth/login.html")
    assert page.evaluate(
        'window.sessionStorage.getItem("mock-signed-out")'
    ) == "true"
    assert messages == []


def test_account_redirects_to_login_without_session(auth_page_loader) -> None:
    page, messages = auth_page_loader("account/index.html")

    page.wait_for_url("http://pages.test/project/auth/login.html")
    assert messages == []
