"""Source files of a repository at one commit, exported once and cached."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from upgrade_impact.eval.git import Git
from upgrade_impact.index.javascript import index_js_repository
from upgrade_impact.index.python import import_names_for, index_repository
from upgrade_impact.models import Bump, Ecosystem, IndexStats, Usage
from upgrade_impact.pipeline import SOURCE_SUFFIXES

# Directories never worth exporting: vendored or generated code.
_SKIP_PARTS = frozenset(
    {"node_modules", "bower_components", "vendor", "third_party", "site-packages", "dist"}
)
MAX_EXPORT_FILES = 20_000


def is_source(path: str) -> bool:
    if not path.endswith(SOURCE_SUFFIXES) or path.endswith((".min.js", ".d.ts")):
        return False
    return not any(part in _SKIP_PARTS for part in path.split("/")[:-1])


def ensure_snapshot(git: Git, cache_dir: Path, repo: str, sha: str) -> Path:
    """Export the source files of ``repo`` at ``sha`` (cached by commit)."""
    dest = cache_dir / "snapshots" / repo.replace("/", "__") / sha
    marker = dest / ".complete"
    if marker.exists():
        return dest
    paths = [p for p in git.list_files(sha) if is_source(p)][:MAX_EXPORT_FILES]
    count = git.export(sha, dest, paths)
    marker.write_text(str(count), encoding="utf-8")
    return dest


@dataclass(slots=True)
class PackageUsages:
    names: list[str]
    usages: list[Usage]
    stats: IndexStats


def package_usages(root: Path, bump: Bump) -> PackageUsages:
    if bump.ecosystem == Ecosystem.PYPI:
        names = import_names_for(bump.package)
        index = index_repository(root, names)
        return PackageUsages(names, index.usages, index.stats)
    names = [bump.package]
    js = index_js_repository(root, names)
    return PackageUsages(names, js.usages, js.stats)
