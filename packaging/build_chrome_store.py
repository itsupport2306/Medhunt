"""Build the minimal, audited Medhunt Chrome Web Store upload ZIP.

Browser code is minified, never obfuscated. All enrichment credentials and
provider orchestration remain in the separately deployed backend.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
import zipfile

from build_frontend import build as build_frontend, release_javascript


FORBIDDEN_CLIENT_TERMS = (
    "people data labs", "people_data_labs", "enformion", "endato",
    "usphonebook", "neverbounce", "twilio", "quick sourcer",
    # Nexus is an intentional user-facing eligibility state in the extension;
    # credentials and API implementation remain backend-only.
    "quick_sourcer", "quick-sourcer", "api_key",
    "client_secret", "access_key", "secret_key",
)
BRAND_REPLACEMENTS = (
    ("RADIXSOL", "MEDHUNT"),
    ("Radixsol", "Medhunt"),
    ("radixsol", "medhunt"),
)
ROOT_PUBLIC_FILES = {
    "app.js", "background.js", "facebook-content.js",
    "healthcare-directory-content.js", "indeed-content.js", "index.html",
    "inject.js", "linkedin-content.js", "manifest.json",
    "platform-content.js", "platform-main.js", "privacy.html", "profile-quality.js",
    "styles.css",
    "icons/medhunt-logo.png", "icons/medhunt-mark.png",
}


def normalize_api_base(value: str, *, template: bool = False) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("The public API base must be an origin-only HTTPS URL.")
    if not template and parsed.hostname.endswith((".invalid", ".example", ".test")):
        raise ValueError("A real deployed API hostname is required for an upload package.")
    return raw


def harden(stage: Path, api_base: str) -> None:
    app_path = stage / "app.js"
    app = app_path.read_text(encoding="utf-8")
    expected = 'const DEFAULT_BACKEND = "http://127.0.0.1:8091";'
    if app.count(expected) != 1:
        raise RuntimeError("Could not locate the development backend marker exactly once.")
    app = app.replace(expected, f'const DEFAULT_BACKEND = {json.dumps(api_base)};')
    local_validation = (
        '  const local = parsed.protocol === "http:" && '
        '["127.0.0.1", "localhost"].includes(parsed.hostname);\n'
        '  if ((!local && parsed.protocol !== "https:") || parsed.username || parsed.password) {\n'
        '    throw new Error("Use HTTP on localhost for development or HTTPS for a hosted backend.");\n'
        '  }'
    )
    public_validation = (
        '  if (parsed.protocol !== "https:" || parsed.username || parsed.password) {\n'
        '    throw new Error("Use an HTTPS hosted backend.");\n'
        '  }'
    )
    if app.count(local_validation) != 1:
        raise RuntimeError("Could not locate the development URL validation exactly once.")
    app = app.replace(local_validation, public_validation)
    app_path.write_text(app, encoding="utf-8", newline="\n")

    manifest_path = stage / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["host_permissions"] = [
        permission for permission in manifest.get("host_permissions", [])
        if permission not in {"http://127.0.0.1/*", "http://localhost/*"}
    ]
    api_permission = f"{api_base}/*"
    if api_permission not in manifest["host_permissions"]:
        manifest["host_permissions"].append(api_permission)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8", newline="\n",
    )

    for path in stage.rglob("*.js"):
        source = path.read_text(encoding="utf-8")
        for old, new in BRAND_REPLACEMENTS:
            source = source.replace(old, new)
        path.write_text(release_javascript(source), encoding="utf-8", newline="\n")


def audit(stage: Path, api_base: str) -> None:
    allowed = set(ROOT_PUBLIC_FILES)
    manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
    allowed.update(manifest.get("icons", {}).values())
    allowed.update(manifest.get("action", {}).get("default_icon", {}).values())
    actual = {
        path.relative_to(stage).as_posix()
        for path in stage.rglob("*") if path.is_file()
    }
    unexpected = sorted(actual - allowed)
    if unexpected:
        raise RuntimeError(f"Unexpected files would be shipped: {', '.join(unexpected)}")

    manifest_hosts = manifest.get("host_permissions", [])
    if "http://127.0.0.1/*" in manifest_hosts or "http://localhost/*" in manifest_hosts:
        raise RuntimeError("Localhost permissions remain in the public manifest.")
    if f"{api_base}/*" not in manifest_hosts:
        raise RuntimeError("The public API permission is missing.")

    for path in stage.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".js", ".json", ".html", ".css"}:
            continue
        content = path.read_text(encoding="utf-8").casefold()
        if "localhost" in content or "127.0.0.1" in content:
            raise RuntimeError(f"Development backend reference found in {path.name}.")
        found = [term for term in FORBIDDEN_CLIENT_TERMS if term in content]
        if found:
            raise RuntimeError(
                f"Private implementation terms found in {path.name}: {', '.join(found)}"
            )
        if re.search(r"(?:sk|pk|api)[_-][A-Za-z0-9_-]{24,}", content, re.I):
            raise RuntimeError(f"Possible credential found in {path.name}.")


def package(source: Path, output: Path, api_base: str) -> None:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="medhunt-chrome-") as temporary:
        stage = Path(temporary) / "extension"
        build_frontend(source, stage)
        harden(stage, api_base)
        audit(stage, api_base)
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(stage).as_posix())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("src_pkg/frontend"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-base", required=True)
    parser.add_argument(
        "--template", action="store_true",
        help="Permit a reserved placeholder domain for a non-uploadable template ZIP.",
    )
    args = parser.parse_args()
    api_base = normalize_api_base(args.api_base, template=args.template)
    package(args.source.resolve(), args.output, api_base)
    print(f"Chrome package created: {args.output.resolve()}")


if __name__ == "__main__":
    main()
