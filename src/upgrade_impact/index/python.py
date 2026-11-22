"""Find where Python code uses a package: imports, calls (with keyword names),
attribute references and bare names, each resolved to a qualified symbol.

``import httpx as h`` then ``h.AsyncClient(timeout=5)`` is recorded as a call
to ``httpx.AsyncClient`` with keyword ``timeout``. Method calls on instances are
resolved when the instance's class is visible in the same file:
``c = httpx.Client()`` / ``with httpx.Client() as c`` / ``def f(c: httpx.Client)``
make ``c.get(url)`` a call to ``httpx.Client.get``. Local modules that re-export
package names (``from .compat import Client``) are followed one hop.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Iterable, Iterator, Mapping
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


def _looks_like_class(symbol: str) -> bool:
    last = symbol.rsplit(".", 1)[-1]
    return last[:1].isupper()


def _annotation_target(node: ast.expr | None) -> ast.expr | None:
    """``httpx.Client``, ``Optional[httpx.Client]``, ``httpx.Client | None`` and
    ``"httpx.Client"`` all name the class ``httpx.Client``."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return _annotation_target(ast.parse(node.value, mode="eval").body)
        except SyntaxError:
            return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        for side in (node.left, node.right):
            if not (isinstance(side, ast.Constant) and side.value is None):
                return _annotation_target(side)
        return None
    if isinstance(node, ast.Subscript):
        head = node.value
        name = head.attr if isinstance(head, ast.Attribute) else getattr(head, "id", "")
        if name == "Optional":
            return _annotation_target(node.slice)
        return None
    if isinstance(node, ast.Attribute | ast.Name):
        return node
    return None


def _target_key(node: ast.expr) -> str | None:
    """``client`` or ``self.client``: the names an instance can be bound to."""
    if isinstance(node, ast.Name):
        return node.id
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in ("self", "cls")
    ):
        return f"{node.value.id}.{node.attr}"
    return None


@dataclass(slots=True)
class _FileIndexer:
    path: str
    lines: list[str]
    prefixes: tuple[str, ...]
    aliases: dict[str, str] = field(default_factory=dict)
    alias_via: dict[str, str] = field(default_factory=dict)
    # Names bound to an instance of a package class, and how we know.
    instances: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Local functions whose return annotation is a package class.
    returns: dict[str, str] = field(default_factory=dict)
    usages: list[Usage] = field(default_factory=list)

    def snippet(self, line: int) -> str:
        if 1 <= line <= len(self.lines):
            return self.lines[line - 1].strip()[:MAX_SNIPPET]
        return ""

    def add(
        self,
        node: ast.AST,
        symbol: str,
        kind: UsageKind,
        keywords: list[str],
        via: str | None = None,
    ) -> None:
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
                via=via,
            )
        )

    def collect_imports(
        self, tree: ast.AST, local_exports: Mapping[str, Mapping[str, tuple[str, str]]]
    ) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self._import(node)
            elif isinstance(node, ast.ImportFrom):
                module = _absolute_module(self.path, node)
                if module is None:
                    continue
                if node.level == 0 and _shares_root(module, self.prefixes):
                    self._import_from(node, module)
                elif module in local_exports:
                    self._import_reexport(node, module, local_exports[module])

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
        for alias in node.names:
            if alias.name == "*":
                if _within(module, self.prefixes):
                    self.add(node, f"{module}.*", UsageKind.IMPORT, [])
                continue
            qualified = f"{module}.{alias.name}"
            self.aliases[alias.asname or alias.name] = qualified
            if _within(qualified, self.prefixes):
                self.add(node, qualified, UsageKind.IMPORT, [])

    def _import_reexport(
        self, node: ast.ImportFrom, module: str, exports: Mapping[str, tuple[str, str]]
    ) -> None:
        """``from .compat import Client`` where compat.py did ``from httpx import Client``."""
        for alias in node.names:
            if alias.name not in exports:
                continue
            qualified, origin = exports[alias.name]
            local = alias.asname or alias.name
            via = f"re-exported by {origin}"
            self.aliases[local] = qualified
            self.alias_via[local] = via
            self.add(node, qualified, UsageKind.IMPORT, [], via=via)

    def resolve(self, node: ast.expr, *, through_instance: bool = True) -> tuple[str, str | None]:
        """The qualified package symbol an expression names, and how it was resolved.

        Returns ``("", None)`` when the expression is not the package's.
        """
        parts: list[str] = []
        current: ast.expr = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        parts.reverse()
        base: str | None = None
        via: str | None = None
        if isinstance(current, ast.Name):
            if through_instance and parts:
                key2 = f"{current.id}.{parts[0]}"
                if key2 in self.instances and len(parts) > 1:
                    base, via = self.instances[key2]
                    parts = parts[1:]
                elif current.id in self.instances:
                    base, via = self.instances[current.id]
            if base is None and current.id in self.aliases:
                base = self.aliases[current.id]
                via = self.alias_via.get(current.id)
        elif isinstance(current, ast.Call) and through_instance and parts:
            # httpx.Client().get(...)
            made, made_via = self.resolve(current.func, through_instance=False)
            if made and _looks_like_class(made):
                base, via = made, made_via or f"instance created at line {current.lineno}"
        if base is None:
            return "", None
        qualified = ".".join([base, *parts])
        return (qualified, via) if _within(qualified, self.prefixes) else ("", None)

    def _instance_of(self, value: ast.expr | None) -> str | None:
        if isinstance(value, ast.Await):
            value = value.value
        if not isinstance(value, ast.Call):
            return None
        symbol, _ = self.resolve(value.func, through_instance=False)
        if symbol and _looks_like_class(symbol):
            return symbol
        if isinstance(value.func, ast.Name) and value.func.id in self.returns:
            return self.returns[value.func.id]
        return None

    def _bind(self, target: ast.expr, symbol: str, how: str, line: int) -> None:
        key = _target_key(target)
        if key is not None:
            self.instances[key] = (symbol, f"{key} {how} {symbol} at line {line}")

    def _annotated_class(self, annotation: ast.expr | None) -> str | None:
        target = _annotation_target(annotation)
        symbol = self.resolve(target, through_instance=False)[0] if target else ""
        return symbol if symbol and _looks_like_class(symbol) else None

    def collect_instances(self, tree: ast.AST) -> None:
        """Bind names to package classes, file-wide (no flow or scope analysis).

        ``c = httpx.Client()``, ``with httpx.Client() as c``, ``self.c = ...``,
        ``def f(c: httpx.Client)``, ``c: httpx.Client = ...`` and
        ``c = make_client()`` where ``def make_client() -> httpx.Client``.
        """
        functions = [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        for func in functions:
            if made := self._annotated_class(func.returns):
                self.returns[func.name] = made
        for func in functions:
            args = func.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                if made := self._annotated_class(arg.annotation):
                    self.instances[arg.arg] = (
                        made,
                        f"{arg.arg} is annotated {made} at line {arg.lineno}",
                    )
        for node in ast.walk(tree):
            self._bind_statement(node)

    def _bind_statement(self, node: ast.AST) -> None:
        if isinstance(node, ast.Assign):
            if made := self._instance_of(node.value):
                for target in node.targets:
                    self._bind(target, made, "=", node.lineno)
        elif isinstance(node, ast.AnnAssign):
            made = self._instance_of(node.value) or self._annotated_class(node.annotation)
            if made:
                self._bind(node.target, made, "is", node.lineno)
        elif isinstance(node, ast.With | ast.AsyncWith):
            for item in node.items:
                made = self._instance_of(item.context_expr)
                if made and item.optional_vars is not None:
                    self._bind(item.optional_vars, made, "is a", node.lineno)

    def collect_usages(self, tree: ast.AST) -> None:
        parents: dict[int, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[id(child)] = parent
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                symbol, via = self.resolve(node.func)
                if symbol:
                    keywords = [kw.arg if kw.arg else "**" for kw in node.keywords]
                    if any(isinstance(arg, ast.Starred) for arg in node.args):
                        keywords.append("*")
                    self.add(node, symbol, UsageKind.CALL, keywords, via=via)
                continue
            if not isinstance(node, ast.Attribute | ast.Name):
                continue
            if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                continue
            owner = parents.get(id(node))
            if isinstance(owner, ast.Attribute) and owner.value is node and self.resolve(owner)[0]:
                continue
            if isinstance(owner, ast.Call) and owner.func is node:
                continue
            if isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
                continue
            # A bare instance (passing `client` around) is not a use of the class name.
            symbol, via = self.resolve(node, through_instance=isinstance(node, ast.Attribute))
            if symbol:
                kind = UsageKind.ATTRIBUTE if isinstance(node, ast.Attribute) else UsageKind.NAME
                self.add(node, symbol, kind, [], via=via)


def _module_name(path: str) -> list[str]:
    """``app/compat.py`` -> ``["app", "compat"]``; ``app/__init__.py`` -> ``["app"]``."""
    parts = path.removesuffix(".pyi").removesuffix(".py").split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return parts


def _absolute_module(path: str, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    parts = _module_name(path)
    package = parts if path.endswith(("__init__.py", "__init__.pyi")) else parts[:-1]
    if node.level - 1 > len(package):
        return None
    base = package[: len(package) - (node.level - 1)]
    if node.module:
        base = [*base, *node.module.split(".")]
    return ".".join(base) if base else None


def _module_keys(path: str) -> list[str]:
    """Dotted names a file can be imported as (``src/`` layouts included)."""
    parts = _module_name(path)
    keys = [".".join(parts)] if parts else []
    if len(parts) > 1 and parts[0] in ("src", "lib"):
        keys.append(".".join(parts[1:]))
    return keys


def _module_exports(tree: ast.Module, prefixes: tuple[str, ...]) -> dict[str, str]:
    """Top-level ``from pkg import X`` / ``import pkg as p`` bindings of one module."""
    exports: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                qualified = f"{node.module}.{alias.name}"
                if alias.name != "*" and _within(qualified, prefixes):
                    exports[alias.asname or alias.name] = qualified
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and _within(alias.name, prefixes):
                    exports[alias.asname] = alias.name
    return exports


@dataclass(slots=True)
class PythonIndex:
    usages: list[Usage]
    stats: IndexStats


def _parse(source: str, name: str, stats: IndexStats) -> ast.Module | None:
    try:
        return ast.parse(source, filename=name)
    except (SyntaxError, ValueError):
        stats.parse_errors += 1
        return None


def _read_sources(
    root: Path, roots: set[str], stats: IndexStats
) -> tuple[dict[str, tuple[str, ast.Module]], list[tuple[str, str]]]:
    """Parsed files that mention the package, and the unparsed rest."""
    parsed: dict[str, tuple[str, ast.Module]] = {}
    others: list[tuple[str, str]] = []
    for path in iter_python_files(root):
        stats.files_scanned += 1
        relative = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            stats.parse_errors += 1
            continue
        if not any(r in source for r in roots):
            others.append((relative, source))
        elif (tree := _parse(source, relative, stats)) is not None:
            parsed[relative] = (source, tree)
    return parsed, others


def index_repository(root: Path, import_names: Iterable[str]) -> PythonIndex:
    prefixes = tuple(dict.fromkeys(import_names))
    stats = IndexStats(import_names=list(prefixes))
    parsed, others = _read_sources(root, {p.split(".", 1)[0] for p in prefixes}, stats)

    # Local modules that re-export package names (compat shims, __init__ barrels).
    local_exports: dict[str, dict[str, tuple[str, str]]] = {}
    for relative, (_, tree) in parsed.items():
        if exports := _module_exports(tree, prefixes):
            for key in _module_keys(relative):
                local_exports[key] = {k: (v, relative) for k, v in exports.items()}
    names = {n for exports in local_exports.values() for n in exports}
    for relative, source in others:
        wanted = names and "import" in source and any(n in source for n in names)
        if wanted and (other := _parse(source, relative, stats)) is not None:
            parsed[relative] = (source, other)

    usages: list[Usage] = []
    for relative in sorted(parsed):
        source, tree = parsed[relative]
        indexer = _FileIndexer(path=relative, lines=source.splitlines(), prefixes=prefixes)
        indexer.collect_imports(tree, local_exports)
        if indexer.aliases:
            indexer.collect_instances(tree)
            indexer.collect_usages(tree)
        if indexer.usages:
            stats.files_importing += 1
            indexer.usages.sort(key=lambda u: (u.line, u.column))
            usages.extend(indexer.usages)
    stats.usages = len(usages)
    return PythonIndex(usages=usages, stats=stats)
