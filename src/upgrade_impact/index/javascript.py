"""Find where JavaScript and TypeScript code uses an npm package, with tree-sitter.

Recorded, each resolved to a qualified symbol such as ``axios.get`` or
``@tanstack/react-query.useQuery``:

- imports: ESM default, named, namespace and type imports, ``require``
  (including destructuring), dynamic ``import()``, and re-exports
  (``export { x } from "pkg"``, ``export * from "pkg"``);
- calls and ``new`` expressions, with the keys of object-literal arguments as
  keywords, so ``axios.get(url, { timeout: 5 })`` passes ``timeout`` and a
  spread (``{ ...opts }``) is recorded as ``**`` like Python's ``**kwargs``;
- JSX elements, with their attribute names as keywords
  (``<QueryClientProvider client={qc}>``);
- member references and bare references, including TypeScript type references;
- method calls on instances made with ``new`` in the same file
  (``const qc = new QueryClient(); qc.clear()`` -> ``QueryClient.clear``);
- local barrel files that re-export package names, followed one hop.

Files under ``node_modules``, build output and minified bundles are skipped.
"""

from __future__ import annotations

import logging
import posixpath
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from upgrade_impact.models import IndexStats, Usage, UsageKind

log = logging.getLogger(__name__)

JS_SUFFIXES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx")
SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "bower_components",
        "dist",
        "build",
        "out",
        "coverage",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".turbo",
        ".cache",
        ".yarn",
        "vendor",
        "__pycache__",
    }
)
MAX_FILE_BYTES = 1_000_000
MAX_FILES = 20_000
MAX_SNIPPET = 200
_RESOLVE_SUFFIXES = ("", *JS_SUFFIXES, *(f"/index{s}" for s in JS_SUFFIXES))


@cache
def _parser(suffix: str) -> Parser:
    if suffix == ".tsx":
        language = Language(tree_sitter_typescript.language_tsx())
    elif suffix in (".ts", ".mts", ".cts"):
        language = Language(tree_sitter_typescript.language_typescript())
    else:
        language = Language(tree_sitter_javascript.language())
    return Parser(language)


def iter_js_files(root: Path) -> Iterator[Path]:
    count = 0
    for path in sorted(p for p in root.rglob("*") if p.suffix in JS_SUFFIXES):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts[:-1]):
            continue
        if path.name.endswith((".min.js", ".bundle.js")):
            continue
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            continue
        count += 1
        if count > MAX_FILES:
            log.warning("stopping after %d JavaScript/TypeScript files", MAX_FILES)
            return
        yield path


def _text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def _string_value(node: Node | None) -> str | None:
    """The value of a string literal node (``"axios"`` -> ``axios``)."""
    if node is None or node.type not in ("string", "template_string"):
        return None
    if node.type == "template_string" and any(
        c.type == "template_substitution" for c in node.children
    ):
        return None
    return _text(node)[1:-1]


def _package_of(specifier: str, packages: Iterable[str]) -> str | None:
    """The package a module specifier belongs to, or ``None``.

    ``axios`` -> ``axios``; ``lodash/fp`` -> ``lodash/fp``; ``node:fs`` -> ``None``.
    """
    for package in packages:
        if specifier == package or specifier.startswith(f"{package}/"):
            return specifier
    return None


def _join(base: str, *parts: str) -> str:
    return ".".join([base, *parts])


MAX_OPTION_DEPTH = 3


def _object_keys(node: Node, depth: int = 1) -> list[str]:
    """Keys of an object literal, including nested option objects
    (``{ defaultOptions: { queries: { cacheTime } } }`` -> all three names)."""
    keys: list[str] = []
    for child in node.named_children:
        if child.type == "pair":
            key = child.child_by_field_name("key")
            if key is not None:
                value = _string_value(key)
                keys.append(value if value is not None else _text(key))
            nested = child.child_by_field_name("value")
            if nested is not None and nested.type == "object" and depth < MAX_OPTION_DEPTH:
                keys.extend(k for k in _object_keys(nested, depth + 1) if k != "**")
        elif child.type in ("shorthand_property_identifier", "method_definition"):
            name = child.child_by_field_name("name") if child.type == "method_definition" else child
            keys.append(_text(name))
        elif child.type == "spread_element":
            keys.append("**")
    return keys


def _call_keywords(arguments: Node | None) -> list[str]:
    if arguments is None:
        return []
    keywords: list[str] = []
    for arg in arguments.named_children:
        if arg.type == "object":
            keywords.extend(dict.fromkeys(_object_keys(arg)))
        elif arg.type == "spread_element":
            keywords.append("*")
    return keywords


def _jsx_keywords(element: Node) -> list[str]:
    keywords: list[str] = []
    for child in element.named_children:
        if child.type == "jsx_attribute":
            name = child.named_children[0] if child.named_children else None
            keywords.append(_text(name))
        elif child.type == "jsx_expression":  # {...props}
            keywords.append("**")
    return keywords


_DECLARATION_PARENTS = frozenset(
    {
        "import_specifier",
        "import_clause",
        "namespace_import",
        "export_specifier",
        "object_pattern",
        "pair_pattern",
        "shorthand_property_identifier_pattern",
        "required_parameter",
        "optional_parameter",
        "formal_parameters",
        "class_declaration",
        "function_declaration",
        "method_definition",
        "type_alias_declaration",
        "interface_declaration",
        "nested_type_identifier",
    }
)


@dataclass(slots=True)
class _FileIndexer:
    path: str
    lines: list[str]
    packages: tuple[str, ...]
    reexports: Mapping[str, Mapping[str, tuple[str, str]]]
    aliases: dict[str, str] = field(default_factory=dict)
    alias_via: dict[str, str] = field(default_factory=dict)
    instances: dict[str, tuple[str, str]] = field(default_factory=dict)
    exports: dict[str, str] = field(default_factory=dict)
    usages: list[Usage] = field(default_factory=list)

    def add(
        self, node: Node, symbol: str, kind: UsageKind, keywords: list[str], via: str | None = None
    ) -> None:
        row, column = node.start_point
        line = row + 1
        snippet = self.lines[row].strip()[:MAX_SNIPPET] if row < len(self.lines) else ""
        self.usages.append(
            Usage(
                path=self.path,
                line=line,
                column=column + 1,
                symbol=symbol,
                kind=kind,
                keywords=keywords,
                snippet=snippet,
                via=via,
            )
        )

    # -- imports -----------------------------------------------------------------

    def _local_module(self, specifier: str) -> Mapping[str, tuple[str, str]] | None:
        if not specifier.startswith("."):
            return None
        base = posixpath.normpath(posixpath.join(posixpath.dirname(self.path), specifier))
        for suffix in _RESOLVE_SUFFIXES:
            if f"{base}{suffix}" in self.reexports:
                return self.reexports[f"{base}{suffix}"]
        return None

    def _bind(self, local: str, qualified: str, via: str | None = None) -> None:
        self.aliases[local] = qualified
        if via:
            self.alias_via[local] = via

    def _import_statement(self, node: Node) -> None:
        source = _string_value(node.child_by_field_name("source"))
        if source is None:
            return
        package = _package_of(source, self.packages)
        local = None if package else self._local_module(source)
        if package is None and local is None:
            return
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        if package is not None and clause is None:  # import "pkg/polyfill"
            self.add(node, package, UsageKind.IMPORT, [])
            return
        for child in clause.named_children if clause is not None else []:
            if child.type == "identifier":  # default import
                if package is not None:
                    self._bind(_text(child), package)
                    self.add(node, package, UsageKind.IMPORT, [])
                elif local is not None and "default" in local:
                    self._reexported(node, _text(child), local["default"])
            elif child.type == "namespace_import":
                name = next((c for c in child.named_children if c.type == "identifier"), None)
                if package is not None and name is not None:
                    self._bind(_text(name), package)
                    self.add(node, package, UsageKind.IMPORT, [])
            elif child.type == "named_imports":
                self._named_imports(child, package, local)

    def _named_imports(
        self, node: Node, package: str | None, local: Mapping[str, tuple[str, str]] | None
    ) -> None:
        for spec in node.named_children:
            if spec.type != "import_specifier":
                continue
            imported = _text(spec.child_by_field_name("name"))
            alias = _text(spec.child_by_field_name("alias")) or imported
            if package is not None:
                self._bind(alias, _join(package, imported))
                self.add(spec, _join(package, imported), UsageKind.IMPORT, [])
            elif local is not None and imported in local:
                self._reexported(spec, alias, local[imported])

    def _reexported(self, node: Node, alias: str, target: tuple[str, str]) -> None:
        qualified, origin = target
        via = f"re-exported by {origin}"
        self._bind(alias, qualified, via)
        self.add(node, qualified, UsageKind.IMPORT, [], via=via)

    def _export_statement(self, node: Node) -> None:
        source = _string_value(node.child_by_field_name("source"))
        clause = next((c for c in node.named_children if c.type == "export_clause"), None)
        package = _package_of(source, self.packages) if source is not None else None
        if source is not None and package is None:
            return
        if package is not None and clause is None:  # export * from "pkg"
            self.add(node, f"{package}.*", UsageKind.IMPORT, [])
            return
        for spec in clause.named_children if clause is not None else []:
            if spec.type != "export_specifier":
                continue
            name = _text(spec.child_by_field_name("name"))
            alias = _text(spec.child_by_field_name("alias")) or name
            if package is not None:
                qualified = _join(package, name) if name != "default" else package
                self.exports[alias] = qualified
                self.add(spec, qualified, UsageKind.IMPORT, [])
            elif name in self.aliases:  # import { x } from "pkg"; export { x }
                self.exports[alias] = self.aliases[name]

    def _require_target(self, node: Node | None) -> tuple[str, list[str]] | None:
        """``require("pkg")`` / ``require("pkg").a.b`` / ``await import("pkg")``."""
        trail: list[str] = []
        while node is not None and node.type in ("member_expression", "await_expression"):
            if node.type == "member_expression":
                trail.append(_text(node.child_by_field_name("property")))
                node = node.child_by_field_name("object")
            else:
                node = node.named_children[0] if node.named_children else None
        if node is None or node.type != "call_expression":
            return None
        func = node.child_by_field_name("function")
        if func is None or _text(func) not in ("require", "import"):
            return None
        args = node.child_by_field_name("arguments")
        first = args.named_children[0] if args is not None and args.named_children else None
        source = _string_value(first)
        package = _package_of(source, self.packages) if source is not None else None
        if package is None:
            return None
        return package, list(reversed(trail))

    def _declarator(self, node: Node) -> None:
        name = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        target = self._require_target(value)
        if target is not None and name is not None:
            package, trail = target
            base = _join(package, *trail) if trail else package
            self.add(node, base, UsageKind.IMPORT, [])
            if name.type == "identifier":
                self._bind(_text(name), base)
            elif name.type == "object_pattern":
                for prop in name.named_children:
                    if prop.type == "shorthand_property_identifier_pattern":
                        self._bind(_text(prop), _join(base, _text(prop)))
                    elif prop.type == "pair_pattern":
                        key = _text(prop.child_by_field_name("key"))
                        val = prop.child_by_field_name("value")
                        if val is not None and val.type == "identifier":
                            self._bind(_text(val), _join(base, key))

    def collect_imports(self, root: Node) -> None:
        for node in _walk(root):
            if node.type == "import_statement":
                self._import_statement(node)
            elif node.type == "variable_declarator":
                self._declarator(node)
            elif node.type == "call_expression" and self._require_target(node) is not None:
                parent = node.parent
                if parent is None or parent.type not in (
                    "variable_declarator",
                    "member_expression",
                    "await_expression",
                ):
                    target = self._require_target(node)
                    if target is not None:
                        self.add(node, target[0], UsageKind.IMPORT, [])
        for node in _walk(root):
            if node.type == "export_statement":
                self._export_statement(node)

    # -- usages ------------------------------------------------------------------

    def resolve(
        self, node: Node | None, *, through_instance: bool = True
    ) -> tuple[str, str | None]:
        parts: list[str] = []
        while node is not None and node.type in (
            "member_expression",
            "nested_identifier",
            "nested_type_identifier",
            "non_null_expression",
            "parenthesized_expression",
        ):
            if node.type in ("non_null_expression", "parenthesized_expression"):
                node = node.named_children[0] if node.named_children else None
                continue
            if node.type == "member_expression":
                prop = node.child_by_field_name("property")
                parts.append(_text(prop))
                node = node.child_by_field_name("object")
            else:  # a.b.C in a type position
                children = node.named_children
                parts.append(_text(children[-1]))
                node = children[0] if children else None
        parts.reverse()
        if node is None:
            return "", None
        name = _text(node)
        if node.type == "this" and parts:
            key = f"this.{parts[0]}"
            if through_instance and key in self.instances and len(parts) > 1:
                base, via = self.instances[key]
                return _join(base, *parts[1:]), via
            return "", None
        if node.type not in ("identifier", "type_identifier", "jsx_identifier"):
            return "", None
        if through_instance and parts and name in self.instances:
            base, via = self.instances[name]
            return _join(base, *parts), via
        if name in self.aliases:
            return _join(self.aliases[name], *parts), self.alias_via.get(name)
        return "", None

    def collect_instances(self, root: Node) -> None:
        """``const qc = new QueryClient()`` and ``this.qc = new QueryClient()``."""
        for node in _walk(root):
            if node.type == "variable_declarator":
                target, value = node.child_by_field_name("name"), node.child_by_field_name("value")
            elif node.type == "assignment_expression":
                target, value = node.child_by_field_name("left"), node.child_by_field_name("right")
            else:
                continue
            while value is not None and value.type in ("await_expression", "as_expression"):
                value = value.named_children[0] if value.named_children else None
            if value is None or value.type != "new_expression" or target is None:
                continue
            made, _ = self.resolve(value.child_by_field_name("constructor"), through_instance=False)
            if not made:
                continue
            key = _text(target)
            if target.type == "identifier" or (
                target.type == "member_expression"
                and key.startswith("this.")
                and key.count(".") == 1
            ):
                line = node.start_point[0] + 1
                self.instances[key] = (made, f"{key} = new {made} at line {line}")

    def collect_usages(self, root: Node) -> None:
        for node in _walk(root):
            kind = node.type
            if kind in ("call_expression", "new_expression"):
                func = node.child_by_field_name(
                    "function" if kind == "call_expression" else "constructor"
                )
                symbol, via = self.resolve(func)
                if symbol:
                    keywords = _call_keywords(node.child_by_field_name("arguments"))
                    self.add(node, symbol, UsageKind.CALL, keywords, via=via)
            elif kind in ("jsx_opening_element", "jsx_self_closing_element"):
                symbol, via = self.resolve(node.child_by_field_name("name"))
                if symbol:
                    self.add(node, symbol, UsageKind.CALL, _jsx_keywords(node), via=via)
            elif kind in ("member_expression", "nested_type_identifier"):
                if self._is_handled_by_parent(node):
                    continue
                symbol, via = self.resolve(node)
                if symbol:
                    self.add(node, symbol, UsageKind.ATTRIBUTE, [], via=via)
            elif kind in ("identifier", "type_identifier"):
                if self._is_handled_by_parent(node) or self._is_declaration(node):
                    continue
                # A bare instance (passing `qc` around) is not a use of the class name.
                symbol, via = self.resolve(node, through_instance=False)
                if symbol:
                    self.add(node, symbol, UsageKind.NAME, [], via=via)

    def _is_handled_by_parent(self, node: Node) -> bool:
        parent = node.parent
        if parent is None:
            return False
        if parent.type == "call_expression" and parent.child_by_field_name("function") == node:
            return True
        if parent.type == "new_expression" and parent.child_by_field_name("constructor") == node:
            return True
        if parent.type in (
            "jsx_opening_element",
            "jsx_self_closing_element",
            "jsx_closing_element",
        ):
            return (
                parent.child_by_field_name("name") == node or parent.type == "jsx_closing_element"
            )
        if parent.type == "nested_type_identifier":
            return True  # the whole a.b.C type is recorded once
        if parent.type == "member_expression" and parent.child_by_field_name("object") == node:
            return bool(self.resolve(parent)[0])  # recorded as part of the longer chain
        return False

    def _is_declaration(self, node: Node) -> bool:
        parent = node.parent
        if parent is None:
            return True
        if parent.type in _DECLARATION_PARENTS:
            return True
        if parent.type == "variable_declarator" and parent.child_by_field_name("name") == node:
            return True
        if parent.type == "pair" and parent.child_by_field_name("key") == node:
            return True
        return parent.type in ("import_statement", "export_statement")


def _walk(root: Node) -> Iterator[Node]:
    cursor = root.walk()
    visited_children = False
    while True:
        if not visited_children:
            node = cursor.node
            if node is not None:
                yield node
            if cursor.goto_first_child():
                continue
        if cursor.goto_next_sibling():
            visited_children = False
        elif cursor.goto_parent():
            visited_children = True
        else:
            return


@dataclass(slots=True)
class JsIndex:
    usages: list[Usage]
    stats: IndexStats


def index_js_repository(root: Path, packages: Iterable[str]) -> JsIndex:
    names = tuple(dict.fromkeys(packages))
    stats = IndexStats(import_names=list(names))
    sources: dict[str, tuple[bytes, Node]] = {}
    for path in iter_js_files(root):
        stats.files_scanned += 1
        try:
            data = path.read_bytes()
        except OSError:
            stats.parse_errors += 1
            continue
        tree = _parser(path.suffix).parse(data)
        if tree.root_node.has_error:
            # tree-sitter recovers; usages outside the broken region are still found.
            stats.parse_errors += 1
        sources[path.relative_to(root).as_posix()] = (data, tree.root_node)

    def run(relative: str, reexports: Mapping[str, Mapping[str, tuple[str, str]]]) -> _FileIndexer:
        data, node = sources[relative]
        indexer = _FileIndexer(
            path=relative,
            lines=data.decode("utf-8", errors="replace").splitlines(),
            packages=names,
            reexports=reexports,
        )
        indexer.collect_imports(node)
        if indexer.aliases:
            indexer.collect_instances(node)
            indexer.collect_usages(node)
        return indexer

    mentions = [r for r, (data, _) in sources.items() if any(n.encode() in data for n in names)]
    # Pass 1: files that import the package, and what they re-export.
    reexports: dict[str, dict[str, tuple[str, str]]] = {}
    for relative in mentions:
        exported = run(relative, {}).exports
        if exported:
            reexports[relative] = {k: (v, relative) for k, v in exported.items()}
    # Pass 2: index for real, now that barrel files are known.
    candidates = set(mentions)
    if reexports:
        candidates.update(
            r for r, (data, _) in sources.items() if b"import" in data or b"require" in data
        )
    usages: list[Usage] = []
    for relative in sorted(candidates):
        indexer = run(relative, reexports)
        if indexer.usages:
            stats.files_importing += 1
            indexer.usages.sort(key=lambda u: (u.line, u.column))
            usages.extend(indexer.usages)
    stats.usages = len(usages)
    return JsIndex(usages=usages, stats=stats)
