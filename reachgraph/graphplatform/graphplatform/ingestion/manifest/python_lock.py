"""Resolved-version parsing for the two Python pin sources this phase
supports: poetry.lock (a real lockfile -- full resolved closure, direct and
transitive) and requirements.txt (not a lockfile; only its exactly-pinned
`==` lines represent a genuinely *resolved* version -- ranges and unpinned
entries are declared, not resolved, and are skipped rather than guessed at).
"""

from __future__ import annotations

import re
import tomllib


def parse_poetry_lock(content: str) -> dict[str, str]:
    doc = tomllib.loads(content)
    resolved: dict[str, str] = {}
    for pkg in doc.get("package", []):
        name = pkg.get("name")
        version = pkg.get("version")
        if name and version:
            resolved[name] = version
    return resolved


_REQ_PIN_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*==\s*([A-Za-z0-9._+!-]+)\s*(;.*)?$")


def parse_requirements_txt(content: str) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue  # blank, comment, or an option like -r/-e/-i/--hash
        m = _REQ_PIN_RE.match(line)
        if not m:
            continue  # not an exact pin (a range, a VCS/URL ref, ...) -- not a resolved version
        resolved[m.group(1)] = m.group(3)
    return resolved
