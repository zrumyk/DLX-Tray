"""Starts a DLXProcess, prints ``<base_url> <dlx pid> <own pid>``, then waits to be killed.

Used by the orphan tests. Usage: ``python dlx_holder.py [command ...]``; without a command it
runs the real binary.
"""

from __future__ import annotations

import os
import sys
import time

from dlx_tray.services.deeplx_process import DLXProcess


def main() -> None:
    command = sys.argv[1:] or None
    process = DLXProcess(command, startup_timeout=15)
    process.start()
    print(process.base_url, process.pid, os.getpid(), flush=True)
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
