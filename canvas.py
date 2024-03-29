#!/usr/bin/env python3

import argparse
import curses
import datetime
import json
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
from crontab import CronTab
from rich.progress import Progress

BASE_URL = ""
CONFIG_FILE = os.path.join(os.path.dirname(__file__), ".config.json")
PAGE_LIMIT = 10000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

logger = logging.getLogger("canvas_logger")
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.propagate = False


def setup_cron_job(script_path, add_job):
    cron = CronTab(user=True)
    job_command = f'{sys.executable} {script_path} --debug run > {os.path.join(os.path.dirname(__file__), "canvas.log")} 2>&1'

    # Find existing job
    existing_job = None
    for job in cron:
        if job.command == job_command:
            existing_job = job
            break

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


class ProgressTracker:
    def __init__(self, progress):
        self.progress = progress
        self.task_ids = {}
        self.lock = threading.Lock()

    def add_course_task(self, course_name, total):
        with self.lock:
            task_id = self.progress.add_task(
                f"Downloading files for {course_name}", total=total
            )
            self.task_ids[course_name] = task_id
            return task_id

    def advance_course_task(self, course_name):
        with self.lock:
            task_id = self.task_ids.get(course_name)
            if task_id is not None:
                self.progress.update(task_id, advance=1)


def is_edited_since(filename, timestamp: datetime.datetime):
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


def is_changed_since(filename, timestamp: datetime.datetime):
    if not os.path.exists(filename):
        return True
    file_modified = os.path.getmtime(filename) < timestamp.timestamp()
    logger.debug(f"File {filename} changed since check: {file_modified}")
    return file_modified


def change_last_modified(filename, timestamp: datetime.datetime):
    if not os.path.exists(filename):
        logger.debug(
            f"Cannot change last modified time. File {filename} does not exist."
        )
        return
    os.utime(filename, (timestamp.timestamp(), timestamp.timestamp()))
    logger.debug(f"Changed last modified time for {filename} to {timestamp}")


def update_configs(key, value):
    configs = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as file:
            configs = json.load(file)
            logger.debug("Loaded existing config file.")
    configs[key] = value
    with open(CONFIG_FILE, "w") as file:
        json.dump(configs, file)
        logger.debug(f"Updated {key} in config file.")


def get_configs(key):
    if not os.path.exists(CONFIG_FILE):
        logger.debug(f"Config file {CONFIG_FILE} not found.")
        return None
    with open(CONFIG_FILE, "r") as file:
        configs = json.load(file)
        logger.debug(f"Retrieved {key} from config file.")
    return configs.get(key, None)


def get_stored_token():
    token = get_configs("token")
    logger.debug("Retrieved stored token." if token else "No token stored.")
    return token


def store_token(token):
    update_configs("token", token)
    logger.debug("Stored token.")


def get_base_url():
    base_url = get_configs("base_url")
    logger.debug("Retrieved stored base_url." if base_url else "No base_url stored.")
    return base_url


def store_base_url(base_url: str):
    global BASE_URL
    BASE_URL = base_url
    update_configs("base_url", base_url)
    logger.debug("Stored base_url.")


def get_download_path():
    path = get_configs("download_path")
    if path:
        logger.debug(f"Retrieved stored download path: {path}")
    return path


def store_download_path(path):
    update_configs("download_path", path)
    logger.debug(f"Stored download path: {path}")


def create_session(token):
    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + token})
    session.params = {"per_page": PAGE_LIMIT}
    logger.debug("Created session with updated headers.")
    return session


def get_courses(session):
    response = session.get(
        BASE_URL + "/api/v1/courses", params={"per_page": PAGE_LIMIT}
    )
    logger.debug(f"Retrieved courses. Status code: {response.status_code}")
    return response.json() if response.status_code == 200 else []


def get_stored_selections():
    selections = get_configs("selections") or {}
    logger.debug("Retrieved stored selections.")
    return selections


def store_selections(selections):
    update_configs("selections", selections)
    logger.debug("Stored selections.")


def curses_select_courses(screen: curses.window, courses):
    current_row = 0
    stored_selections = get_stored_selections()
    selections = {
        course["id"]: stored_selections.get(str(course["id"]), False)
        for course in courses
    }

    def print_menu(row):
        screen.clear()
        screen.addstr("Select courses to track:\n\n")

        for idx, course in enumerate(courses):
            if selections.get(course["id"], False):
                selected_indicator = "[✔]"
            else:
                selected_indicator = "[ ]"

            if idx == row:
                screen.addstr(
                    f"{selected_indicator} {course['name']}\n", curses.A_REVERSE
                )
            else:
                screen.addstr(f"{selected_indicator} {course['name']}\n")

        screen.addstr(
            f"\n{len([course for course in courses if selections.get(course['id'], False)])} courses selected\n"
        )
        screen.addstr("Press q to quit\n")
        screen.refresh()

    while True:
        print_menu(current_row)
        key = screen.getch()

        if key == curses.KEY_UP and current_row > 0:
            current_row -= 1
        elif key == curses.KEY_DOWN and current_row < len(courses) - 1:
            current_row += 1
        elif key == curses.KEY_ENTER or key in [10, 13]:
            course_id = courses[current_row]["id"]
            selections[course_id] = not selections[course_id]
        elif key == ord("q"):
            break

    store_selections({str(k): v for k, v in selections.items()})


def get_folder_list(session: requests.Session, course_id: str):
    response = session.get(
        BASE_URL + f"/api/v1/courses/{course_id}/folders",
        params={"per_page": PAGE_LIMIT},
    )
    return response.json()


def clean_url(url):
    o = urlparse(url)
    return o.scheme + "://" + o.netloc


def validate_url(url):
    parsed_url = urlparse(url)
    return parsed_url.scheme in ("http", "https")


def prompt_for_input(prompt, validator=None, default=None):
    while True:
        user_input = input(prompt) or default
        if not validator or validator(user_input):
            return user_input
        else:
            print("Invalid input. Please try again.")


def setup():
    global BASE_URL, BASE_DIR
    configs = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as file:
            configs = json.load(file)

    # Canvas URL
    default_url = configs.get("base_url", BASE_URL)
    prompt = "Enter your Canvas URL"
    prompt += f" [{default_url}]: " if default_url else ": "
    base_url = prompt_for_input(prompt, validator=validate_url, default=default_url)
    base_url = clean_url(base_url)
    store_base_url(base_url)
    BASE_URL = base_url

    # OAuth Token
    default_token = configs.get("token", "")
    token_prompt = "Enter your OAuth token"
    token_prompt += " [previous token]: " if default_token else ": "
    token = prompt_for_input(token_prompt, default=default_token) or default_token
    store_token(token)

    # Download Path
    default_path = configs.get("download_path", BASE_DIR)
    prompt = f"Enter path to download files [{default_path}]: "
    download_path = prompt_for_input(prompt, default=default_path)
    while not os.path.exists(download_path):
        print(f"Invalid path: {download_path}. ", end="")
        download_path = prompt_for_input(prompt, default=default_path)
    store_download_path(download_path)
    BASE_DIR = download_path

    # Cron Job Setup
    cron_setup_choice = prompt_for_input(
        "Set up a cron job to run every 2 hours? (y/n) [n]: ", default="n"
    ).lower()
    script_path = os.path.abspath(__file__)
    setup_cron_job(script_path, add_job=cron_setup_choice == "y")

    # Course Selection
    session = create_session(token)
    courses = [course for course in get_courses(session) if "name" in course]
    curses.wrapper(curses_select_courses, courses)
    stored_selections = get_stored_selections()
    courses_to_track = [
        course for course in courses if stored_selections.get(str(course["id"]), False)
    ]
    for course in courses_to_track:
        course_path = os.path.join(download_path, course["name"])
        if not os.path.exists(course_path):
            os.makedirs(course_path)

    script_name = os.path.basename(sys.argv[0])
    run_command = f"python {script_name} run"
    print(
        f"\nSetup complete! Please run \033[1m{run_command}\033[0m to start tracking."
    )


def download_file(
    session: requests.Session,
    file: dict,
    folder_name: str,
    course_name: str,
    progress_tracker: ProgressTracker,
):
    logger.debug(f"Downloading file from {course_name}")

    # Clean up the folder names
    clean_course_name = course_name.replace(" ", "_")
    clean_folder_name = folder_name.replace(" ", "_")
    clean_folder_name = "/".join(clean_folder_name.split("/")[1:])

    # Create the folders if it doesn't exist
    dir = os.path.join(BASE_DIR, clean_course_name, clean_folder_name)
    if not os.path.exists(dir):
        os.makedirs(dir, exist_ok=True)

    file_url = file.get("url", "")
    display_name = file.get("display_name", "")
    full_file_path = os.path.join(
        BASE_DIR, clean_course_name, clean_folder_name, display_name
    )

    updated_at_str = file.get("updated_at", "0000-00-00T00:00:00Z")
    updated_dt = datetime.datetime.strptime(updated_at_str, "%Y-%m-%dT%H:%M:%SZ")

    if is_edited_since(full_file_path, updated_dt):
        progress_tracker.advance_course_task(course_name)
        logger.debug(f"Downloaded file from {course_name}")
        return

    if not is_changed_since(full_file_path, updated_dt):
        progress_tracker.advance_course_task(course_name)
        logger.debug(f"Downloaded file from {course_name}")
        return

    file_content = session.get(file_url)
    if file_content.status_code != 200:
        logger.debug(
            f"Failed to download {full_file_path}, status_code: {file_content.status_code}"
        )
        progress_tracker.advance_course_task(course_name)
        logger.debug(f"Downloaded file from {course_name}")
        return

    with open(full_file_path, "wb") as f:
        f.write(file_content.content)

    logger.debug(f"Downloaded {full_file_path}")
    logger.debug(f"Downloaded file from {course_name}")
    change_last_modified(full_file_path, updated_dt)
    progress_tracker.advance_course_task(course_name)


def get_files_and_download(
    session, files_url, folder_name, course_name, progress_tracker
):
    files = session.get(files_url, params={"per_page": PAGE_LIMIT}).json()
    files_seen = set()
    files_to_download = []
    for file in sorted(files, key=lambda f: f["updated_at"], reverse=True):
        if file["display_name"] in files_seen:
            continue
        files_seen.add(file["display_name"])
        files_to_download.append(file)

    with ThreadPoolExecutor() as executor:
        logger.debug(
            f"{course_name}'s folder has {len(files_to_download)} files to download"
        )
        for file in files_to_download:
            executor.submit(
                download_file, session, file, folder_name, course_name, progress_tracker
            )


def process_course(session, course, progress_tracker):
    folders = get_folder_list(session, course["id"])
    num_files = 0
    for folder in folders:
        files = session.get(folder["files_url"], params={"per_page": PAGE_LIMIT}).json()
        num_files += len(set(file["display_name"] for file in files))

    logger.debug(f"{course['name']} has {num_files} files")
    progress_tracker.add_course_task(course["name"], num_files)

    with ThreadPoolExecutor() as executor:
        for folder in folders:
            course_name = course["name"]
            folder_name = folder["full_name"]
            files_url = folder["files_url"]
            executor.submit(
                get_files_and_download,
                session,
                files_url,
                folder_name,
                course_name,
                progress_tracker,
            )


def run():
    global BASE_URL, BASE_DIR
    base_url = get_base_url()
    if not base_url:
        print("No base_url found. Please run python canvas.py setup to get started.")
        return
    BASE_URL = base_url

    token = get_stored_token()
    if not token:
        print("No token found. Please run python canvas.py setup to get started.")
        return

    base_dir = get_download_path()
    if not base_dir:
        print(
            "No download path found. Please run python canvas.py setup to get started."
        )
        return
    BASE_DIR = base_dir

    session = create_session(token)
    courses = [course for course in get_courses(session) if "name" in course]
    stored_selections = get_stored_selections()
    courses_to_track = [
        course for course in courses if stored_selections.get(str(course["id"]), False)
    ]

    logger.debug(
        "Courses to track: "
        + ", ".join([course["name"] for course in courses_to_track])
    )

    print("Downloading files...\n")

    with Progress() as progress:
        progress_tracker = ProgressTracker(progress)

        with ThreadPoolExecutor() as executor:
            for course in courses_to_track:
                executor.submit(process_course, session, course, progress_tracker)

    print("\nDownload process completed for all courses.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["setup", "run"])
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
    else:
        logger.setLevel(logging.WARNING)

    if args.command == "setup":
        setup()
    elif args.command == "run":
        run()


if __name__ == "__main__":
    main()
