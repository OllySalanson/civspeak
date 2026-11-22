"""The packaged app's entry point (Civspeak.exe): the same as `python -m civspeak`."""

import sys

from civspeak.cli import main

if __name__ == "__main__":
    sys.exit(main())
