"""Fail if requirements.txt names a package requirements.lock does not pin.

The Dockerfile installs from the lock, not from requirements.txt, so adding a
dependency and forgetting to regenerate the lock produces an image that is
missing it — and nothing else notices, because CI's own test job installs from
requirements.txt and stays green.

Regenerate with:
    docker run --rm -v "$PWD/requirements.txt:/r.txt:ro" python:3.12-slim \\
      sh -c 'pip install -q -r /r.txt 2>/dev/null && pip freeze' 2>/dev/null \\
      | grep -E '^[A-Za-z0-9_.-]+==' > requirements.lock
"""

import pathlib
import re


def normalise(name: str) -> str:
    """PEP 503 normalisation: Pydantic_Settings and pydantic-settings are one."""
    return re.sub(r"[-_.]+", "-", name).lower()


def top_level(path: pathlib.Path) -> set[str]:
    """Package names from a requirements file, minus extras and specifiers."""
    names = set()
    for raw in path.read_text().splitlines():
        line = raw.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        names.add(normalise(re.split(r"[\[><=!~;]", line)[0].strip()))
    return names


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    wanted = top_level(root / "requirements.txt")
    pinned = top_level(root / "requirements.lock")

    missing = sorted(wanted - pinned)
    if missing:
        print("requirements.lock is stale — these are in requirements.txt but not pinned:")
        for name in missing:
            print(f"  {name}")
        print("\nRegenerate it (see this file's docstring), then commit the result.")
        return 1

    print(
        f"requirements.lock covers all {len(wanted)} direct dependencies "
        f"({len(pinned)} pinned in total)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
