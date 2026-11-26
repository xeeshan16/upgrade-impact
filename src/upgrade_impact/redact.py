"""Redact secrets from code lines before they are shown to a model.

The review step sends numbered windows of the user's code to the configured
model, which may be a hosted API. The calls it reviews are calls into an HTTP
or SDK client more often than not, and those are exactly the lines where a
hard-coded ``Authorization: Bearer ...`` header, an API key or a connection
string with a password sits. So every source line read for a code window goes
through :func:`redact_line` first; the prompt, the citation check and the
report all see the redacted text, so a model quoting a redacted line still
verifies.

Rules are specific: known token formats, credentials in URLs, authorization
headers, private key markers, and assignments of a *string literal* to a
secret-sounding name (``api_key = "..."``, ``"password": "..."``). Values that
are not literals (``token = os.environ["TOKEN"]``) are left alone, and so are
placeholders (``"changeme"``, ``"<token>"``, ``"${TOKEN}"``). Line numbers and
everything else on the line are unchanged; only the secret becomes
``[REDACTED:<kind>]``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MARKER = "[REDACTED:{kind}]"


@dataclass(frozen=True, slots=True)
class Rule:
    kind: str
    pattern: re.Pattern[str]


def _rule(kind: str, pattern: str, flags: int = 0) -> Rule:
    return Rule(kind, re.compile(pattern, flags))


# Specific token formats first, so `token = "ghp_..."` is reported as a GitHub token.
RULES: tuple[Rule, ...] = (
    _rule(
        "github_token",
        r"(?P<secret>\bgh[pousr]_[A-Za-z0-9]{36,255}\b|\bgithub_pat_[A-Za-z0-9_]{22,255}\b)",
    ),
    _rule("gitlab_token", r"(?P<secret>\bglpat-[A-Za-z0-9_\-]{20,}\b)"),
    _rule("aws_access_key_id", r"(?P<secret>\b(?:AKIA|ASIA)[0-9A-Z]{16}\b)"),
    _rule("slack_token", r"(?P<secret>\bxox[abposr]-[A-Za-z0-9-]{10,}\b)"),
    _rule("slack_webhook", r"(?P<secret>https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]{20,})"),
    _rule("google_api_key", r"(?P<secret>\bAIza[0-9A-Za-z_\-]{35}\b)"),
    _rule("stripe_key", r"(?P<secret>\b(?:sk|rk)_live_[0-9A-Za-z]{16,}\b)"),
    _rule("llm_api_key", r"(?P<secret>\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,})"),
    _rule("npm_token", r"(?P<secret>\bnpm_[A-Za-z0-9]{36}\b)"),
    _rule(
        "jwt",
        r"(?P<secret>\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,})",
    ),
    _rule("private_key", r"(?P<secret>-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*)"),
    # scheme://user:password@host  (database URLs, brokers, proxies, git remotes)
    _rule(
        "url_credentials",
        r"\b[a-z][a-z0-9+.\-]*://[^/\s:@'\"]+:(?P<secret>[^@\s/'\"]{3,})@",
        re.IGNORECASE,
    ),
    _rule(
        "authorization_header",
        r"\bauthorization[\"']?\s*[:=,]\s*[\"'`]?(?:bearer|basic|token|bot)\s+"
        r"(?P<secret>[A-Za-z0-9._~+/=\-]{8,})",
        re.IGNORECASE,
    ),
    # A string literal assigned to (or keyed by) a secret-sounding name:
    # api_key = "...", "password": "...", clientSecret: '...', token="..."
    _rule(
        "credential_literal",
        r"(?P<key>\b[A-Za-z0-9_.\-]{0,40}?"
        r"(?:password|passwd|pwd|passphrase|secret|token|api[_\-]?key|apikey|access[_\-]?key"
        r"|private[_\-]?key|auth[_\-]?token|credentials?))"
        r"[\"']?\s*(?::|=|=>)\s*[bfru]{0,2}(?P<quote>[\"'`])(?P<secret>[^\"'`\s]+)(?P=quote)",
        re.IGNORECASE,
    ),
)

_PLACEHOLDER = re.compile(
    r"^(?:\*+|x+|X+|\.+|<[^>]*>|\[[^\]]*\]|\$\{?.*|\{\{.*|%\(?\w+\)?s?|%[A-Za-z_]+%?)$"
)
_PLAIN_VALUES = frozenset(
    {
        "true",
        "false",
        "null",
        "none",
        "changeme",
        "password",
        "secret",
        "token",
        "example",
        "test",
        "dummy",
        "redacted",
    }
)


def _looks_secret(value: str) -> bool:
    """For ``name = "literal"``: skip placeholders and short or plain words."""
    if _PLACEHOLDER.match(value) or value.lower() in _PLAIN_VALUES:
        return False
    if len(value) >= 16:
        return True
    has_alpha = any(c.isalpha() for c in value)
    has_digit = any(c.isdigit() for c in value)
    return len(value) >= 8 and has_alpha and has_digit


def redact_line(text: str) -> tuple[str, list[str]]:
    """``text`` with secrets replaced, and the kind of each redaction."""
    kinds: list[str] = []
    for rule in RULES:

        def substitute(match: re.Match[str], rule: Rule = rule) -> str:
            secret = match.group("secret")
            if secret.startswith("[REDACTED:"):
                return match.group(0)
            if rule.kind == "credential_literal" and not _looks_secret(secret):
                return match.group(0)
            kinds.append(rule.kind)
            start, end = match.span("secret")
            offset = match.start(0)
            whole = match.group(0)
            return whole[: start - offset] + MARKER.format(kind=rule.kind) + whole[end - offset :]

        text = rule.pattern.sub(substitute, text)
    return text, kinds
