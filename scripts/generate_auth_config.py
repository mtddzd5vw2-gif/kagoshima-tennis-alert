from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit


REQUIRED_VARIABLES = (
    "SUPABASE_URL",
    "SUPABASE_PUBLISHABLE_KEY",
    "AUTH_CALLBACK_URL",
)
FORBIDDEN_KEY_MARKERS = (
    "sb_secret_",
    "service_role",
    "service-role",
    "service role",
)


def require_value(environment: dict[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"Required environment variable is empty: {name}")
    return value


def validate_public_url(value: str, name: str, *, allow_path: bool) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{name} must not contain credentials, a query, or a fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError(f"{name} must use HTTPS except on localhost")
    if not allow_path and parsed.path not in {"", "/"}:
        raise ValueError(f"{name} must be a Supabase project origin without a path")


def decode_jwt_payload(value: str) -> dict[str, object] | None:
    parts = value.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + ("=" * (-len(parts[1]) % 4))
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        parsed = json.loads(decoded.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError, binascii.Error):
        return None
    return parsed if isinstance(parsed, dict) else None


def validate_publishable_key(value: str) -> None:
    normalized = value.casefold()
    if any(marker in normalized for marker in FORBIDDEN_KEY_MARKERS):
        raise ValueError("SUPABASE_PUBLISHABLE_KEY contains a secret/service-role marker")

    payload = decode_jwt_payload(value)
    if payload and payload.get("role") == "service_role":
        raise ValueError("SUPABASE_PUBLISHABLE_KEY must not be a service-role JWT")
    if normalized.startswith("sb_publishable_"):
        return
    if payload and payload.get("role") == "anon":
        return
    raise ValueError(
        "SUPABASE_PUBLISHABLE_KEY must be a publishable key or legacy anon JWT"
    )


def javascript_string(value: str) -> str:
    encoded = json.dumps(value, ensure_ascii=False)
    return (
        encoded.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def build_config(environment: dict[str, str]) -> str:
    values = {name: require_value(environment, name) for name in REQUIRED_VARIABLES}
    validate_public_url(values["SUPABASE_URL"], "SUPABASE_URL", allow_path=False)
    validate_public_url(
        values["AUTH_CALLBACK_URL"],
        "AUTH_CALLBACK_URL",
        allow_path=True,
    )
    validate_publishable_key(values["SUPABASE_PUBLISHABLE_KEY"])

    return (
        "/* Generated public browser configuration. Do not edit or commit. */\n"
        "window.TCW_AUTH_CONFIG = Object.freeze({\n"
        f"  supabaseUrl: {javascript_string(values['SUPABASE_URL'])},\n"
        "  supabasePublishableKey: "
        f"{javascript_string(values['SUPABASE_PUBLISHABLE_KEY'])},\n"
        f"  authCallbackUrl: {javascript_string(values['AUTH_CALLBACK_URL'])},\n"
        "});\n"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the public Supabase browser configuration."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("assets/config/auth-config.js"),
        help="Output path (default: assets/config/auth-config.js)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        content = build_config(dict(os.environ))
    except ValueError as error:
        print(f"auth config generation failed: {error}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8", newline="\n")
    print(f"Generated public auth config: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
