"""Command-line entry point: `pitpat [SPEED]` or `python -m pitpat [SPEED]`."""
import argparse
import logging
from logging.handlers import RotatingFileHandler

from .app import PitPatApp
from .config import CONFIG_PATH, Config, app_dir
from .protocol import VARIANTS


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pitpat",
        description="Control a PitPat treadmill from the terminal.",
        epilog=f"Settings (last treadmill, presets) are kept in {CONFIG_PATH}",
    )
    parser.add_argument("speed", nargs="?", type=float, help="start the belt at this speed once connected")
    parser.add_argument("-a", "--address", help="connect to this treadmill instead of the last one used")
    parser.add_argument("--scan", action="store_true", help="pick a treadmill from a scan instead of the last one used")
    parser.add_argument("--variant", choices=list(VARIANTS), help="force a hardware variant instead of detecting it")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every status frame")
    args = parser.parse_args()

    # The terminal belongs to the UI, so logs only go to a file.
    log_file = app_dir() / "pitpat.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, handlers=[handler])

    config = Config.load()
    if config.variant not in VARIANTS:
        config.variant = None
    if args.scan:
        config.address = None
    if args.address:
        if args.address.upper() != (config.address or "").upper():
            config.variant = None
        config.address = args.address
    if args.variant:
        config.variant = args.variant

    PitPatApp(config, startup_speed=args.speed).run()


if __name__ == "__main__":
    main()
