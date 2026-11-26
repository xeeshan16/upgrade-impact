from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from upgrade_impact.adjudicate import SourceFiles
from upgrade_impact.config import Settings
from upgrade_impact.models import Bump, Ecosystem, NotesSection, ReleaseNotes
from upgrade_impact.pipeline import Target, check_bump
from upgrade_impact.providers.fake import ScriptedChat
from upgrade_impact.redact import redact_line

# Built from parts so the file itself holds no token-shaped strings.
GITHUB = "ghp_" + "a1B2" * 9
OPENAI = "sk-proj-" + "Zx9" * 10
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"


@pytest.mark.parametrize(
    ("line", "kind", "secret"),
    [
        (f'client = httpx.Client(headers={{"Authorization": "Bearer {OPENAI}"}})', "llm_api_key",
         OPENAI),
        (f"const gh = new Octokit({{ auth: '{GITHUB}' }})", "github_token", GITHUB),
        (f"aws_key = '{AWS}'", "aws_access_key_id", AWS),
        ('engine = create_engine("postgresql://app:s3cretPass@db/app")', "url_credentials",
         "s3cretPass"),
        ('headers = {"Authorization": "Bearer abcdef1234567890"}', "authorization_header",
         "abcdef1234567890"),
        ('API_KEY = "f00dfeed12345678"', "credential_literal", "f00dfeed12345678"),
        ("clientSecret: 'q8w7e6r5t4y3'", "credential_literal", "q8w7e6r5t4y3"),
        ('db_password="hunter2hunter2"', "credential_literal", "hunter2hunter2"),
        ("-----BEGIN RSA PRIVATE KEY-----", "private_key", "-----BEGIN RSA PRIVATE KEY-----"),
    ],
)  # fmt: skip
def test_secrets_are_redacted(line: str, kind: str, secret: str) -> None:
    clean, kinds = redact_line(line)
    assert secret not in clean
    assert kind in kinds
    assert f"[REDACTED:{kind}]" in clean


@pytest.mark.parametrize(
    "line",
    [
        'token = os.environ["GITHUB_TOKEN"]',
        "api_key = settings.OPENAI_API_KEY",
        'password = "changeme"',
        'token = "<your token>"',
        'secret = "${SECRET}"',
        "TOKEN_TTL = 3600",
        'client = httpx.Client(timeout=5, proxies="http://proxy:3128")',
        'sha = "0123456789abcdef0123456789abcdef01234567"',
        "def get_token(self) -> str:",
    ],
)
def test_ordinary_code_is_untouched(line: str) -> None:
    assert redact_line(line) == (line, [])


def test_redaction_is_idempotent() -> None:
    once, _ = redact_line(f'API_KEY = "{OPENAI}"')
    assert redact_line(once) == (once, [])


def test_source_files_redact_and_count(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(f'import httpx\nKEY = "{OPENAI}"\n', encoding="utf-8")
    files = SourceFiles(tmp_path)
    assert files.window("app.py", 1) == [(1, "import httpx"), (2, 'KEY = "[REDACTED:llm_api_key]"')]
    assert files.redacted == {"llm_api_key": 1}


def test_review_prompt_never_carries_the_secret(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "import httpx\n\n"
        f'HEADERS = {{"Authorization": "Bearer {OPENAI}"}}\n'
        "client = httpx.Client(proxies=None, headers=HEADERS)\n",
        encoding="utf-8",
    )
    bump = Bump(
        ecosystem=Ecosystem.PYPI, package="httpx", from_version="0.27", to_version="0.28",
        source="x",
    )  # fmt: skip
    notes = ReleaseNotes.from_sections(
        "httpx",
        [
            NotesSection(
                version="0.28.0",
                source="changelog",
                lines=["* The deprecated `proxies` argument has now been removed."],
            )
        ],
    )
    extraction = {
        "breaking_lines": [1],
        "claims": [
            {
                "evidence": [{"line": 1, "quote": "`proxies` argument has now been removed"}],
                "summary": "proxies= was removed",
                "kind": "removed",
                "symbols": [],
                "parameters": ["proxies"],
            }
        ],
    }
    chat = ScriptedChat([json.dumps(extraction), json.dumps({"decisions": []})])
    report = check_bump(
        bump,
        target=Target(source="t", bumps=[bump]),
        code_root=tmp_path,
        chat=chat,
        registry=None,  # type: ignore[arg-type]
        github=None,  # type: ignore[arg-type]
        settings=Settings(llm_cache="off", recheck="off"),
        import_names=["httpx"],
        notes=notes,
    )
    assert len(chat.requests) == 2  # extraction, then the review of the possible match
    review_prompt = "\n".join(m.content for m in chat.requests[1])
    assert "client = httpx.Client(proxies=None" in review_prompt
    assert OPENAI not in review_prompt
    assert "[REDACTED:llm_api_key]" in review_prompt
    assert any("redacted secrets in the code read for review" in w for w in report.warnings)


def test_long_lines_do_not_backtrack() -> None:
    # A minified bundle line: long runs of identifier characters, quotes and separators.
    line = ("a_b-c.d" * 400) + ' = "x"' + ("token_" * 300)
    started = time.perf_counter()
    redact_line(line)
    assert time.perf_counter() - started < 0.5
