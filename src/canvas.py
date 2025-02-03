#!/usr/bin/env python3
import argparse
import logging

import trio

from commands import run, setup
from config import CanvasConfig
from logger import setup_logger

logger = setup_logger()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["setup", "run"])
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
    else:
        logger.setLevel(logging.WARNING)

    config = CanvasConfig()
    config.load()

    if args.command == "setup":
        await setup(config)
    elif args.command == "run":
        await run(config)

if __name__ == "__main__":
    trio.run(main)
