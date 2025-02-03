import datetime
import logging
import os
import sys
from typing import Callable
from urllib.parse import urlparse

from crontab import CronTab

logger = logging.getLogger("canvas_logger")


def setup_cron_job(script_path: str, add_job: bool) -> None:
    cron = CronTab(user=True)
    job_command = f"{sys.executable} {script_path} --debug run > {os.path.join(os.path.dirname(__file__), 'canvas.log')} 2>&1"

    existing_job = next((job for job in cron if job.command == job_command), None)

    if add_job and not existing_job:
        job = cron.new(command=job_command)
        job.minute.on(0)
        job.hour.every(2)
        cron.write()
        print("Cron job setup complete. The script will run every 2 hours.")
    elif not add_job and existing_job:
        cron.remove(existing_job)
        cron.write()
        print("Existing cron job removed.")


def is_edited_since(filename: str, timestamp: datetime.datetime) -> bool:
    if not os.path.exists(filename):
        logger.debug(f"File {filename} does not exist.")
        return False
    file_modified = os.path.getmtime(filename) > timestamp.timestamp()
    logger.debug(f"File {filename} edited since check: {file_modified}")
    if file_modified:
        logger.debug(
            f"File was edited at {os.path.getmtime(filename)}, given timestamp is {timestamp.timestamp()}"
        )
    return file_modified


def is_changed_since(filename: str, timestamp: datetime.datetime) -> bool:
    if not os.path.exists(filename):
        return True
    file_modified = os.path.getmtime(filename) < timestamp.timestamp()
    logger.debug(f"File {filename} changed since check: {file_modified}")
    return file_modified


def change_last_modified(filename: str, timestamp: datetime.datetime) -> None:
    if not os.path.exists(filename):
        logger.debug(
            f"Cannot change last modified time. File {filename} does not exist."
        )
        return
    os.utime(filename, (timestamp.timestamp(), timestamp.timestamp()))
    logger.debug(f"Changed last modified time for {filename} to {timestamp}")


def escape_path(path: str) -> str:
    return path.replace(os.path.sep, "_").replace(" ", "_")


def clean_url(url: str) -> str:
    o = urlparse(url)
    return f"{o.scheme}://{o.netloc}"


def validate_url(url: str) -> bool:
    parsed_url = urlparse(url)
    return parsed_url.scheme in ("http", "https")


def prompt_for_input(
    prompt: str,
    validator: Callable[[str], bool] | None = None,
    default: str | None = None,
) -> str:
    while True:
        user_input = input(prompt) or default
        if user_input and (not validator or validator(user_input)):
            return user_input
        else:
            print("Invalid input. Please try again.")
