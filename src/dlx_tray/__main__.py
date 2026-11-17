"""Entry point: ``python -m dlx_tray`` or the ``dlx-tray`` script."""

import sys

from dlx_tray.app import run


def main() -> int:
    return run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
