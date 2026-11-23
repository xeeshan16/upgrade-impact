"""Builds a small local git history shaped like real Dependabot traffic, for tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

BOT = ("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com")
HUMAN = ("Maintainer", "maintainer@example.com")


def git(cwd: Path, *args: str, author: tuple[str, str] = HUMAN) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": author[0],
        "GIT_AUTHOR_EMAIL": author[1],
        "GIT_COMMITTER_NAME": author[0],
        "GIT_COMMITTER_EMAIL": author[1],
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit(root: Path, message: str, author: tuple[str, str] = HUMAN) -> str:
    git(root, "add", "-A", author=author)
    git(root, "commit", "-q", "-m", message, author=author)
    return git(root, "rev-parse", "HEAD").strip()


def build_history(base: Path, slug: str = "acme/app") -> Path:
    """Create ``base/work`` with six bump PRs and a bare copy at ``base/remote/<slug>.git``.

    * PR #7 (merge commit): httpx 0.27.2 -> 0.28.0, then a human fixes ``app/main.py`` line 3.
    * PR #8 (squash by the bot): requests 1.0.0 -> 2.0.0, manifest only.
    * PR #9 (squash with a human co-author): rich 12.0.0 -> 13.0.0, ``app/ui.py`` changed.
    * PR #10 (squash): a patch bump, skipped as not notable.
    * PR #11 (squash): click 7 -> 8, clean, but a later human commit fixes it: skipped.
    * PR #12 (squash): black, which no file imports: skipped.
    """
    work = base / "work"
    work.mkdir(parents=True)
    git(work, "init", "-q", "-b", "main")
    write(work, "requirements.txt", "httpx==0.27.2\nrequests==1.0.0\nrich==12.0.0\nidna==3.6\n")
    write(
        work,
        "app/main.py",
        "import httpx\n\n"
        "client = httpx.AsyncClient(app=object(), base_url='http://test')\n"
        "other = httpx.get('http://example.com')\n",
    )
    write(work, "app/net.py", "import requests\n\nresponse = requests.get('http://x')\n")
    write(work, "app/ui.py", "from rich.console import Console\n\nConsole().print('hi')\n")
    write(work, "app/idn.py", "import idna\n")
    write(work, "app/cli.py", "import click\n\nclick.echo('x')\n")
    commit(work, "Initial commit")

    git(work, "checkout", "-q", "-b", "dependabot/pip/httpx-0.28.0")
    write(work, "requirements.txt", "httpx==0.28.0\nrequests==1.0.0\nrich==12.0.0\nidna==3.6\n")
    commit(work, "Bump httpx from 0.27.2 to 0.28.0", BOT)
    write(
        work,
        "app/main.py",
        "import httpx\n\n"
        "client = httpx.AsyncClient(transport=httpx.ASGITransport(app=object()), "
        "base_url='http://test')\n"
        "other = httpx.get('http://example.com')\n",
    )
    commit(work, "Use ASGITransport")
    git(work, "checkout", "-q", "main")
    git(
        work,
        "merge",
        "-q",
        "--no-ff",
        "-m",
        "Merge pull request #7 from acme/dependabot/pip/httpx-0.28.0",
        "dependabot/pip/httpx-0.28.0",
    )

    write(work, "requirements.txt", "httpx==0.28.0\nrequests==2.0.0\nrich==12.0.0\nidna==3.6\n")
    commit(work, "Bump requests from 1.0.0 to 2.0.0 (#8)", BOT)

    write(work, "requirements.txt", "httpx==0.28.0\nrequests==2.0.0\nrich==13.0.0\nidna==3.6\n")
    write(work, "app/ui.py", "from rich.console import Console\n\nConsole().log('hi')\n")
    commit(
        work,
        "Bump rich from 12.0.0 to 13.0.0 (#9)\n\nCo-authored-by: Jane Dev <jane@example.com>",
        BOT,
    )

    write(work, "requirements.txt", "httpx==0.28.0\nrequests==2.0.0\nrich==13.0.0\nidna==3.7\n")
    commit(work, "Bump idna from 3.6 to 3.7 (#10)", BOT)

    write(
        work,
        "requirements.txt",
        "httpx==0.28.0\nrequests==2.0.0\nrich==13.0.0\nidna==3.7\nclick==8.0\n",
    )
    commit(work, "Bump click from 7.0 to 8.0 (#11)", BOT)
    write(work, "app/cli.py", "import click\n\nclick.secho('x')\n")
    commit(work, "Fix click 8 compatibility")

    write(work, "requirements-dev.txt", "black==24.0.0\n")
    commit(work, "Bump black from 23.0.0 to 24.0.0 (#12)", BOT)

    remote = base / "remote" / f"{slug}.git"
    remote.parent.mkdir(parents=True)
    git(base, "clone", "-q", "--bare", str(work), str(remote))
    return base / "remote"
