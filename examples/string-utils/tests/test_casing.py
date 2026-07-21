"""Preset tests for ``string_utils.casing.snake_case``.

These pin the *starting* behaviour of the dogfood target — the green baseline
the verifier (``pytest``) regresses against after the v0.4 dogfood run adds
``slugify`` (ticket 07). Boundary cases (empty / non-str) are intentional so
the preset suite is non-trivial.
"""

from __future__ import annotations

import pytest

from string_utils import snake_case


class TestSnakeCase:
    def test_camel_case(self) -> None:
        assert snake_case("CamelCase") == "camel_case"

    def test_space_separated(self) -> None:
        assert snake_case("hello world") == "hello_world"

    def test_kebab_case(self) -> None:
        assert snake_case("already-snake") == "already_snake"

    def test_mixed_separators_collapse_to_single_underscore(self) -> None:
        assert snake_case("Foo  Bar--Baz") == "foo_bar_baz"

    def test_leading_and_trailing_separators_trimmed(self) -> None:
        assert snake_case("  wrap me  ") == "wrap_me"

    def test_empty_string(self) -> None:
        assert snake_case("") == ""

    def test_all_separator_input(self) -> None:
        assert snake_case("---   ---") == ""

    def test_non_ascii_letters_passed_through(self) -> None:
        # No Unicode case-folding: non-ASCII letters survive untouched.
        assert snake_case("café Noir") == "café_noir"

    def test_digits_preserved(self) -> None:
        assert snake_case("route66 Express") == "route66_express"

    def test_non_str_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            snake_case(123)  # type: ignore[arg-type]
