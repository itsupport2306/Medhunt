"""Safely read allowlisted Nexus configuration from an existing project.

The Nexus reference application predates Medhunt and keeps one OAuth client
value as the literal fallback of ``os.getenv`` in ``app/config.py``.  Importing
that module would execute arbitrary application code, so this loader parses the
file as syntax and accepts only the exact string literal used for
``NEXUS_TOKEN_BASIC``.  Nothing in this module logs or returns unrelated
settings.
"""
from __future__ import annotations

import ast
from pathlib import Path

from dotenv import dotenv_values


# Tenant routing is intentionally omitted: the reference app may use the
# Bulk Parsing referral source, while Medhunt must configure its own.
NEXUS_KEYS = (
    "NEXUS_BASE_URL",
    "NEXUS_AUTH_METHOD",
    "NEXUS_TOKEN_URL",
    "NEXUS_TOKEN_PAYLOAD_STYLE",
    "NEXUS_CLIENT_ID",
    "NEXUS_CLIENT_SECRET",
    "NEXUS_USERNAME",
    "NEXUS_PASSWORD",
    "NEXUS_STATIC_TOKEN",
    "NEXUS_TOKEN_BASIC",
    "NEXUS_ORG_CODE",
    "NEXUS_RESUME_DOC_TYPE_ID",
)


def _clean(value: object) -> str:
    return str(value or "").strip()


def _assignment_name(node: ast.Assign | ast.AnnAssign) -> str:
    target = node.target if isinstance(node, ast.AnnAssign) else (
        node.targets[0] if len(node.targets) == 1 else None
    )
    return target.id if isinstance(target, ast.Name) else ""


def _strip_call(node: ast.AST) -> ast.AST:
    if (
        isinstance(node, ast.Call)
        and not node.args
        and not node.keywords
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "strip"
    ):
        return node.func.value
    return node


def _literal_env_default(node: ast.AST, expected_name: str) -> str:
    node = _strip_call(node)
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
        and node.func.attr == "getenv"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == expected_name
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        return ""
    return _clean(node.args[1].value)


def token_basic_literal(config_path: str | Path) -> str:
    """Return only the static ``NEXUS_TOKEN_BASIC`` getenv fallback."""
    path = Path(config_path).expanduser().resolve(strict=True)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    matches: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if _assignment_name(node) != "NEXUS_TOKEN_BASIC":
            continue
        value_node = node.value
        if value_node is None:
            continue
        value = _literal_env_default(value_node, "NEXUS_TOKEN_BASIC")
        if value:
            matches.append(value)
    if len(matches) > 1 and len(set(matches)) > 1:
        raise ValueError("NEXUS_TOKEN_BASIC has multiple literal defaults.")
    return matches[0] if matches else ""


def load_reference(
    env_path: str | Path = "",
    config_path: str | Path = "",
) -> dict[str, str]:
    """Load only Nexus values from the two explicitly configured files."""
    output: dict[str, str] = {}
    if _clean(env_path):
        values = dotenv_values(Path(env_path).expanduser().resolve(strict=True))
        output.update({
            key: value
            for key in NEXUS_KEYS
            if (value := _clean(values.get(key)))
        })
    if _clean(config_path) and not output.get("NEXUS_TOKEN_BASIC"):
        value = token_basic_literal(config_path)
        if value:
            output["NEXUS_TOKEN_BASIC"] = value
    return output


__all__ = ["NEXUS_KEYS", "load_reference", "token_basic_literal"]
