"""``python -m ai_dev`` — thin alias to the ``ai-dev`` console script."""

from ai_dev.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
