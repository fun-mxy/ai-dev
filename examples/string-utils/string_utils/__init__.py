"""``string_utils`` — tiny string-helpers package (ai-dev v0.4 dogfood target).

Public surface is re-exported here so callers can do
``from string_utils import snake_case`` without reaching into the submodule.
"""

from string_utils.casing import snake_case

__all__ = ["snake_case"]
