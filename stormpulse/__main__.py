"""Entry point for python -m stormpulse."""

import sys

from stormpulse import __version__


def main() -> None:
    print(f"storm-pulse-agent v{__version__}")
    sys.exit(0)


if __name__ == "__main__":
    main()
