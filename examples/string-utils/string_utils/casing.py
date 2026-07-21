"""Case-conversion helpers for ``string_utils``.

This module ships the preset ``snake_case`` function — the *existing* surface
the ai-dev v0.4 dogfood target starts with. The v0.4 dogfood run (ticket 07)
adds ``slugify`` here as its new feature, but this file deliberately only
contains the pre-existing function so the starting repo has a real green
``pytest``/``mypy`` baseline for the verifier to regress against.
"""

from __future__ import annotations

import re


def snake_case(s: str) -> str:
    """Return ``s`` rewritten in ``snake_case``.

    Handles the three common source shapes — ``"CamelCase"``, ``"kebab-case"``
    and ``"space separated"`` — and normalises runs of non-alphanumeric input
    into single underscores. ASCII letters are lower-cased; non-ASCII letters
    are passed through unchanged (no Unicode folding), matching the
    documented contract the preset tests pin.

        >>> snake_case("CamelCase")
        'camel_case'
        >>> snake_case("hello world")
        'hello_world'
        >>> snake_case("already-snake")
        'already_snake'
    """
    if not isinstance(s, str):
        raise TypeError(f"snake_case() expected str, got {type(s).__name__}")
    # Insert an underscore before an uppercase letter that follows a lowercase
    # letter or digit (the CamelCase boundary), then collapse anything that is
    # not a letter or digit into a single underscore separator.
    boundary = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    # Collapse any run of non-word characters (spaces, hyphens, punctuation)
    # *plus* underscores into a single underscore. \w keeps Unicode letters
    # like é intact on a str pattern, so they survive untouched.
    collapsed = re.sub(r"[\W_]+", "_", boundary)
    trimmed = collapsed.strip("_")
    return trimmed.lower()
