from __future__ import annotations

from pathlib import Path

from sourcing import nexus_reference


def test_reference_loader_reads_only_allowlisted_values_without_execution(tmp_path: Path):
    reference_env = tmp_path / ".env"
    reference_env.write_text(
        "NEXUS_AUTH_METHOD=password\n"
        "NEXUS_USERNAME=reference-user\n"
        'NEXUS_DEFAULT_PROFILE={"referralSourceId":5638,"jobTypeIds":["TRAVEL"]}\n'
        "UNRELATED_SECRET=must-not-leave-reference\n",
        encoding="utf-8",
    )
    reference_config = tmp_path / "config.py"
    reference_config.write_text(
        "import os\n"
        "NEXUS_TOKEN_BASIC = os.getenv('NEXUS_TOKEN_BASIC', 'safe-test-basic').strip()\n"
        "raise RuntimeError('this file must never execute')\n",
        encoding="utf-8",
    )

    values = nexus_reference.load_reference(reference_env, reference_config)

    assert values == {
        "NEXUS_AUTH_METHOD": "password",
        "NEXUS_USERNAME": "reference-user",
        "NEXUS_TOKEN_BASIC": "safe-test-basic",
    }


def test_reference_env_value_overrides_config_literal(tmp_path: Path):
    reference_env = tmp_path / ".env"
    reference_env.write_text("NEXUS_TOKEN_BASIC=env-basic\n", encoding="utf-8")
    reference_config = tmp_path / "config.py"
    reference_config.write_text(
        "import os\n"
        "NEXUS_TOKEN_BASIC = os.getenv('NEXUS_TOKEN_BASIC', 'code-basic').strip()\n",
        encoding="utf-8",
    )

    assert nexus_reference.load_reference(reference_env, reference_config)[
        "NEXUS_TOKEN_BASIC"
    ] == "env-basic"


def test_nonliteral_python_expression_is_rejected(tmp_path: Path):
    reference_config = tmp_path / "config.py"
    reference_config.write_text(
        "import os\n"
        "DEFAULT = 'do-not-evaluate'\n"
        "NEXUS_TOKEN_BASIC = os.getenv('NEXUS_TOKEN_BASIC', DEFAULT).strip()\n",
        encoding="utf-8",
    )

    assert nexus_reference.token_basic_literal(reference_config) == ""
