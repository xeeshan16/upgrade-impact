"""Find where Python code uses a package: imports, calls (with keyword names),
attribute references and bare names, each resolved to a qualified symbol.

``import httpx as h`` then ``h.AsyncClient(timeout=5)`` is recorded as a call
to ``httpx.AsyncClient`` with keyword ``timeout``. Method calls on instances
(``client.get``) are not resolved here; that needs type information.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from packaging.utils import canonicalize_name

from upgrade_impact.models import IndexStats, Usage, UsageKind

log = logging.getLogger(__name__)

SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        ".env",
        ".tox",
        ".nox",
        "node_modules",
        "site-packages",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "build",
        "dist",
        ".eggs",
        ".cache",
    }
)
MAX_FILE_BYTES = 1_000_000
MAX_FILES = 20_000
MAX_SNIPPET = 200

# Distributions whose import name differs from the project name.
KNOWN_IMPORT_NAMES: dict[str, list[str]] = {
    "pyyaml": ["yaml"],
    "beautifulsoup4": ["bs4"],
    "pillow": ["PIL"],
    "scikit-learn": ["sklearn"],
    "scikit-image": ["skimage"],
    "python-dateutil": ["dateutil"],
    "opencv-python": ["cv2"],
    "opencv-python-headless": ["cv2"],
    "protobuf": ["google.protobuf"],
    "attrs": ["attr", "attrs"],
    "pyjwt": ["jwt"],
    "python-dotenv": ["dotenv"],
    "psycopg2-binary": ["psycopg2"],
    "psycopg-binary": ["psycopg"],
    "djangorestframework": ["rest_framework"],
    "python-multipart": ["multipart", "python_multipart"],
    "pycryptodome": ["Crypto"],
    "pycryptodomex": ["Cryptodome"],
    "pyopenssl": ["OpenSSL"],
    "websocket-client": ["websocket"],
    "pyserial": ["serial"],
    "pyzmq": ["zmq"],
    "faiss-cpu": ["faiss"],
    "faiss-gpu": ["faiss"],
    "google-cloud-storage": ["google.cloud.storage"],
    "google-cloud-bigquery": ["google.cloud.bigquery"],
    "google-api-python-client": ["googleapiclient"],
    "msgpack-python": ["msgpack"],
    "python-jose": ["jose"],
    "python-magic": ["magic"],
    "python-slugify": ["slugify"],
    "ruamel.yaml": ["ruamel.yaml"],
    "typing-extensions": ["typing_extensions"],
    "importlib-metadata": ["importlib_metadata"],
    "pymupdf": ["fitz", "pymupdf"],
    "discord.py": ["discord"],
    "python-telegram-bot": ["telegram"],
    "sentry-sdk": ["sentry_sdk"],
    "grpcio": ["grpc"],
    "mysqlclient": ["MySQLdb"],
    "pywin32": ["win32api", "win32con", "pywintypes"],
    "setuptools": ["setuptools", "pkg_resources"],
}


def import_names_for(package: str) -> list[str]:
    """Likely import names for a PyPI distribution (override with ``--import-name``)."""
    canonical = canonicalize_name(package)
    if canonical in KNOWN_IMPORT_NAMES:
        return KNOWN_IMPORT_NAMES[canonical]
    names = [canonical.replace("-", "_")]
    for prefix in ("python-", "py-"):
        if canonical.startswith(prefix):
            names.append(canonical[len(prefix) :].replace("-", "_"))
    if "-" in canonical:
        names.append(canonical.replace("-", "."))
    return list(dict.fromkeys(names))


def iter_python_files(root: Path) -> Iterator[Path]:
    count = 0
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in relative.parts[:-1]):
            continue
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            continue
        count += 1
        if count > MAX_FILES:
            log.warning("stopping after %d Python files", MAX_FILES)
            return
        yield path


def _within(qualified: str, prefixes: Iterable[str]) -> bool:
    return any(qualified == p or qualified.startswith(f"{p}.") for p in prefixes)


def _shares_root(module: str, prefixes: Iterable[str]) -> bool:
    root = module.split(".", 1)[0]
    return any(p.split(".", 1)[0] == root for p in prefixes)


@dataclass(slots=True)
class _FileIndexer:
    path: str
    lines: list[str]
    prefixes: tuple[str, ...]
    aliases: dict[str, str] = field(default_factory=dict)
    usages: list[Usage] = field(default_factory=list)

    def snippet(self, line: int) -> str:
        if 1 <= line <= len(self.lines):
            return self.lines[line - 1].strip()[:MAX_SNIPPET]
        return ""

    def add(self, node: ast.AST, symbol: str, kind: UsageKind, keywords: list[str]) -> None:
        line = getattr(node, "lineno", 0)
        self.usages.append(
            Usage(
                path=self.path,
                line=line,
                column=getattr(node, "col_offset", 0) + 1,
                symbol=symbol,
                kind=kind,
                keywords=keywords,
                snippet=self.snippet(line),
            )
        )

    def collect_imports(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self._import(node)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                self._import_from(node, node.module)

    def _import(self, node: ast.Import) -> None:
        for alias in node.names:
            if not _shares_root(alias.name, self.prefixes):
                continue
            if alias.asname:
                self.aliases[alias.asname] = alias.name
            else:
                root = alias.name.split(".", 1)[0]
                self.aliases.setdefault(root, root)
            if _within(alias.name, self.prefixes):
                self.add(node, alias.name, UsageKind.IMPORT, [])

    def _import_from(self, node: ast.ImportFrom, module: str) -> None:
        if not _shares_root(module, self.prefixes):
            return
        for alias in node.names:
            if alias.name == "*":
                if _within(module, self.prefixes):
                    self.add(node, f"{module}.*", UsageKind.IMPORT, [])
                continue
            qualified = f"{module}.{alias.name}"
            self.aliases[alias.asname or alias.name] = qualified
            if _within(qualified, self.prefixes):
                self.add(node, qualified, UsageKind.IMPORT, [])

    def resolve(self, node: ast.expr) -> str | None:
        parts: list[str] = []
        current: ast.expr = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if not isinstance(current, ast.Name) or current.id not in self.aliases:
            return None
        parts.append(self.aliases[current.id])
        qualified = ".".join(reversed(parts))
        return qualified if _within(qualified, self.prefixes) else None

    def collect_usages(self, tree: ast.AST) -> None:
        parents: dict[int, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[id(child)] = parent
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                symbol = self.resolve(node.func)
                if symbol:
                    keywords = [kw.arg if kw.arg else "**" for kw in node.keywords]
                    if any(isinstance(arg, ast.Starred) for arg in node.args):
                        keywords.append("*")
                    self.add(node, symbol, UsageKind.CALL, keywords)
                continue
            if not isinstance(node, ast.Attribute | ast.Name):
                continue
            if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                continue
            owner = parents.get(id(node))
            if isinstance(owner, ast.Attribute) and owner.value is node and self.resolve(owner):
                continue
            if isinstance(owner, ast.Call) and owner.func is node:
                continue
            symbol = self.resolve(node)
            if symbol:
                kind = UsageKind.ATTRIBUTE if isinstance(node, ast.Attribute) else UsageKind.NAME
                self.add(node, symbol, kind, [])


@dataclass(slots=True)
class PythonIndex:
    usages: list[Usage]
    stats: IndexStats


def index_repository(root: Path, import_names: Iterable[str]) -> PythonIndex:
    prefixes = tuple(dict.fromkeys(import_names))
    stats = IndexStats(import_names=list(prefixes))
    usages: list[Usage] = []
    for path in iter_python_files(root):
        stats.files_scanned += 1
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            stats.parse_errors += 1
            continue
        if not any(p.split(".", 1)[0] in source for p in prefixes):
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, ValueError):
            stats.parse_errors += 1
            continue
        indexer = _FileIndexer(
            path=path.relative_to(root).as_posix(),
            lines=source.splitlines(),
            prefixes=prefixes,
        )
        indexer.collect_imports(tree)
        if indexer.aliases:
            indexer.collect_usages(tree)
        if indexer.usages:
            stats.files_importing += 1
            indexer.usages.sort(key=lambda u: (u.line, u.column))
            usages.extend(indexer.usages)
    stats.usages = len(usages)
    return PythonIndex(usages=usages, stats=stats)
