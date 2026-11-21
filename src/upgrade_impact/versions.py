"""Version parsing that works for PEP 440 (PyPI) and semver (npm) strings.

``packaging`` already understands most semver strings once a leading ``v`` is
dropped (``1.2.3-beta.1`` normalises to ``1.2.3b1``). Strings it cannot parse
fall back to a numeric tuple so that ordering still mostly works.
"""

from __future__ import annotations

import re
from functools import total_ordering

from packaging.version import InvalidVersion, Version

VERSION_TOKEN = re.compile(
    r"(?<![\w.])v?(\d+(?:\.\d+){0,3}(?:[-.]?(?:a|b|rc|alpha|beta|pre|dev|post|next|canary)"
    r"[-.]?\d*)*(?:\+[\w.]+)?)(?![\w])",
    re.IGNORECASE,
)

# Operators whose version is a version the project may end up on.
_LOWER_OR_PIN = re.compile(r"(===|==|>=|~=|\^|~|>|=)?\s*v?(\d[\w.+-]*)")


@total_ordering
class Ver:
    """A comparable version. Wraps ``packaging.Version`` with a lenient fallback."""

    __slots__ = ("_key", "raw")

    def __init__(self, raw: str) -> None:
        self.raw = raw.strip()
        text = self.raw[1:] if self.raw[:1] in ("v", "V") else self.raw
        try:
            parsed = Version(text)
            self._key: tuple[int, object] = (1, parsed)
        except InvalidVersion:
            numbers = tuple(int(n) for n in re.findall(r"\d+", text)[:4])
            self._key = (0, numbers)

    @property
    def valid(self) -> bool:
        return self._key[0] == 1

    @property
    def is_prerelease(self) -> bool:
        value = self._key[1]
        return isinstance(value, Version) and (value.is_prerelease or value.is_devrelease)

    @property
    def release(self) -> tuple[int, ...]:
        value = self._key[1]
        if isinstance(value, Version):
            return value.release
        assert isinstance(value, tuple)
        return value

    def _cmp_key(self) -> tuple[int, ...] | Version:
        value = self._key[1]
        if isinstance(value, Version):
            return value
        assert isinstance(value, tuple)
        return Version(".".join(str(n) for n in value) or "0")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Ver):
            return NotImplemented
        return self._cmp_key() == other._cmp_key()

    def __lt__(self, other: Ver) -> bool:
        return self._cmp_key() < other._cmp_key()  # type: ignore[operator]

    def __hash__(self) -> int:
        return hash(self._cmp_key())

    def __repr__(self) -> str:
        return f"Ver({self.raw!r})"


def parse(raw: str | None) -> Ver | None:
    if not raw or not re.search(r"\d", raw):
        return None
    return Ver(raw)


def in_range(version: str, start: str | None, end: str) -> bool:
    """``start < version <= end``; with no ``start``, only ``version == end``.

    Pre-releases are excluded unless the target itself is a pre-release.
    """
    v, hi = Ver(version), Ver(end)
    if v.is_prerelease and not hi.is_prerelease:
        return False
    if start is None:
        return v == hi
    return Ver(start) < v <= hi


def version_from_spec(spec: str) -> str | None:
    """The version a requirement specifier points at: ``>=0.27,<0.29`` -> ``0.27``.

    Upper bounds (``<``, ``<=``) and exclusions (``!=``) are ignored; among the
    rest the highest version wins (``^1.2.0 || ^2.0.0`` -> ``2.0.0``).
    """
    candidates: list[Ver] = []
    for part in re.split(r"\s*(?:,|\|\||\s)\s*", spec.strip()):
        if not part or part.startswith(("<", "!")):
            continue
        match = _LOWER_OR_PIN.match(part)
        if not match:
            continue
        token = match.group(2).rstrip(".,;")
        token = token.replace(".*", "").replace(".x", "")
        if re.search(r"\d", token):
            candidates.append(Ver(token))
    if not candidates:
        return None
    return max(candidates).raw


def find_version(text: str) -> str | None:
    """The first version-looking token in a heading or tag."""
    match = VERSION_TOKEN.search(text)
    return match.group(1) if match else None
