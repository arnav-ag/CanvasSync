#!/usr/bin/env python3

import argparse
import curses
import datetime
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TypedDict
from urllib.parse import urlparse

import httpx
import trio
from crontab import CronTab
from rich import print as rprint
from rich.progress import Progress, TaskID
from rich.tree import Tree

CONFIG_FILE: str = os.path.join(os.path.dirname(__file__), ".config.json")
PAGE_LIMIT: int = 10000

logger = logging.getLogger("canvas_logger")
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.propagate = False


class CourseDict(TypedDict):
    id: int
    name: str


class FileDict(TypedDict):
    url: str
    display_name: str
    updated_at: str


class FolderDict(TypedDict):
    files_url: str
    full_name: str


class CanvasAPIError(Exception):
    pass


class CanvasAPI:
    def __init__(self, base_url: str, token: str, page_limit: int = 10000):
        self.base_url = base_url
        self.token = token
        self.page_limit = page_limit
        self.client: Optional[httpx.AsyncClient] = None
        self.logger = logging.getLogger("canvas_logger")

    async def __aenter__(self):
        await self.create_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close_client()

    async def create_client(self):
        self.client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self.token}"})

    async def close_client(self):
        if self.client:
            await self.client.aclose()
            self.client = None

    async def _request(self, method: str, endpoint: str, **kwargs) -> Any:
        if not self.client:
            await self.create_client()

        url = f"{self.base_url}/api/v1/{endpoint}"
        params = kwargs.get('params', {})
        params['per_page'] = self.page_limit

        max_retries = 3
        retry_delay = 1

        for attempt in range(max_retries):
            try:
                self.logger.debug(f"Trying to get URL: {url=} {
                                  method=} {params=} {kwargs=}")
                response = await self.client.request(method, url, params=params, **kwargs)
                if response.status_code == 403:
                    self.logger.debug(response.text)
                    raise CanvasAPIError(
                        "Access forbidden. Check your API token and permissions.")
                # response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:  # Too Many Requests
                    retry_after = int(e.response.headers.get(
                        'Retry-After', retry_delay))
                    self.logger.warning(f"Rate limited. Retrying after {
                                        retry_after} seconds.")
                    await trio.sleep(retry_after)
                elif attempt == max_retries - 1:
                    raise CanvasAPIError(
                        f"HTTP error {e.response.status_code}: {str(e)}")
                else:
                    await trio.sleep(retry_delay * (2 ** attempt))
            except httpx.RequestError as e:
                raise CanvasAPIError(f"Network error: {str(e)}")

        raise CanvasAPIError(
            "Max retries reached. Unable to complete the request.")

    async def get_courses(self) -> List[CourseDict]:
        courses = await self._request('GET', 'courses')
        return [course for course in courses if 'name' in course and 'id' in course]

    async def get_folder_list(self, course_id: str) -> List[Dict[str, Any]]:
        return await self._request('GET', f'courses/{course_id}/folders')

    async def get_files(self, files_url: str) -> List[FileDict]:
        # Extract the relative path from the full URL
        relative_path = files_url.split('/api/v1/')[-1]
        return await self._request('GET', relative_path)

    async def download_file(self, file_url: str) -> bytes:
        if not self.client:
            await self.create_client()

        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                response = await client.get(file_url, headers={"Authorization": f"Bearer {self.token}"})
                response.raise_for_status()
                return response.content
        except httpx.HTTPStatusError as e:
            raise CanvasAPIError(
                f"HTTP error {e.response.status_code} while downloading file: {str(e)}")
        except httpx.RequestError as e:
            raise CanvasAPIError(
                f"Network error while downloading file: {str(e)}")


@dataclass
class CanvasConfig:
    base_url: str = ""
    base_dir: str = os.path.dirname(os.path.abspath(__file__))
    token: str = ""
    download_path: str = ""
    selections: Dict[str, bool] = field(default_factory=dict)
    api: Optional[CanvasAPI] = None

    def load(self) -> None:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as file:
                config_data = json.load(file)
                for key, value in config_data.items():
                    setattr(self, key, value)
        self.api = CanvasAPI(self.base_url, self.token)

    def save(self) -> None:
        with open(CONFIG_FILE, "w") as file:
            json.dump(
                {k: v for k, v in self.__dict__.items() if k != 'api'}, file)

    def update(self, key: str, value: Any) -> None:
        setattr(self, key, value)
        if key in ['base_url', 'token']:
            self.api = CanvasAPI(self.base_url, self.token)
        self.save()


@dataclass
class ProgressTracker:
    progress: Progress
    task_ids: Dict[str, TaskID] = field(default_factory=dict)
    downloaded_files: Dict[str, List[Dict[str, str]]
        ] = field(default_factory=dict)

    def add_course_task(self, course_name: str, total: int) -> TaskID:
        task_id = self.progress.add_task(f"Downloading files for {
                                         course_name}", total=total)
        self.task_ids[course_name] = task_id
        return task_id

    def advance_course_task(self, course_name: str) -> None:
        task_id = self.task_ids.get(course_name)
        if task_id is not None:
            self.progress.update(task_id, advance=1)


def setup_cron_job(script_path: str, add_job: bool) -> None:
    cron = CronTab(user=True)
    job_command = f'{sys.executable} {
        script_path} - -debug run > {os.path.join(os.path.dirname(__file__), "canvas.log")} 2 > &1'

    existing_job = next(
        (job for job in cron if job.command == job_command), None)

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
        logger.debug(f"File was edited at {os.path.getmtime(
            filename)}, given timestamp is {timestamp.timestamp()}")
    return file_modified


def is_changed_since(filename: str, timestamp: datetime.datetime) -> bool:
    if not os.path.exists(filename):
        return True
    file_modified = os.path.getmtime(filename) < timestamp.timestamp()
    logger.debug(f"File {filename} changed since check: {file_modified}")
    return file_modified


def change_last_modified(filename: str, timestamp: datetime.datetime) -> None:
    if not os.path.exists(filename):
        logger.debug(f"Cannot change last modified time. File {
                     filename} does not exist.")
        return
    os.utime(filename, (timestamp.timestamp(), timestamp.timestamp()))
    logger.debug(f"Changed last modified time for {filename} to {timestamp}")


def escape_path(path: str) -> str:
    return path.replace(os.path.sep, '_').replace(" ", "_")


def clean_url(url: str) -> str:
    o = urlparse(url)
    return f"{o.scheme}://{o.netloc}"


def validate_url(url: str) -> bool:
    parsed_url = urlparse(url)
    return parsed_url.scheme in ("http", "https")


def prompt_for_input(
    prompt: str, validator: Optional[callable] = None, default: Optional[str] = None) -> str | None:
    while True:
        user_input = input(prompt) or default
        if user_input and (not validator or validator(user_input)):
            return user_input
        else:
            print("Invalid input. Please try again.")


def curses_select_courses(
    screen: curses.window, courses: List[CourseDict], config: CanvasConfig) -> None:
    current_row = 0
    selections = {
        course["id"]: config.selections.get(str(course["id"]), False)
        for course in courses
    }

    def print_menu(row: int) -> None:
        screen.clear()
        screen.addstr("Select courses to track:\n\n")

        for idx, course in enumerate(courses):
            selected_indicator = "[✔]" if selections.get(
                course["id"], False) else "[ ]"
            if idx == row:
                screen.addstr(f"{selected_indicator} {
                              course['name']}\n", curses.A_REVERSE)
            else:
                screen.addstr(f"{selected_indicator} {course['name']}\n")

        screen.addstr(f"\n{len([course for course in courses if selections.get(
            course['id'], False)])} courses selected\n")
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

    config.update("selections", {str(k): v for k, v in selections.items()})


async def download_file(
    api: CanvasAPI,
    file: FileDict,
    folder_name: str,
    course_name: str,
    progress_tracker: ProgressTracker,
    config: CanvasConfig,
) -> None:
    logger.debug(f"Downloading file from {course_name}")

    clean_course_name = escape_path(course_name.replace(" ", "_"))
    clean_folder_name = folder_name.replace(" ", "_")
    clean_folder_name = "/".join(clean_folder_name.split("/")[1:])

    dir = os.path.join(config.base_dir, clean_course_name, clean_folder_name)
    os.makedirs(dir, exist_ok=True)

    file_url = file.get("url", "")
    display_name = file.get("display_name", "")
    full_file_path = os.path.join(
        config.base_dir, clean_course_name, clean_folder_name, display_name)

    updated_at_str = file.get("updated_at", "0000-00-00T00:00:00Z")
    updated_dt = datetime.datetime.strptime(
        updated_at_str, "%Y-%m-%dT%H:%M:%SZ")

    if is_edited_since(full_file_path, updated_dt) or not is_changed_since(
        full_file_path, updated_dt):
        progress_tracker.advance_course_task(course_name)
        logger.debug(f"Skipped file from {course_name}")
        return

    try:
        file_content = await api.download_file(file_url)
        with open(full_file_path, "wb") as f:
            f.write(file_content)

        progress_tracker.downloaded_files.setdefault(course_name, []).append({
                    "folder": folder_name,
                    "name": display_name
                })

        logger.debug(f"Downloaded {full_file_path}")
        change_last_modified(full_file_path, updated_dt)
        progress_tracker.advance_course_task(course_name)
    except CanvasAPIError as e:
        logger.error(f"Canvas API error while downloading file {
                     display_name} from {course_name}: {str(e)}")
    except Exception as e:
        logger.error(f"Error downloading file {
                     display_name} from {course_name}: {str(e)}")


async def get_files_and_download(
    api: CanvasAPI,
    files_url: str,
    folder_name: str,
    course_name: str,
    progress_tracker: ProgressTracker,
    config: CanvasConfig,
) -> None:
    try:

        if 'CS4212' in course_name:
            print(files_url)
        files = await api.get_files(files_url)

        if not isinstance(files, list):
            logger.error(f"Unexpected response format for files in folder {
                         folder_name} of course {course_name}. Response: {files}")
            return

        files_seen: set[str] = set()
        files_to_download: List[FileDict] = []
        for file in sorted(files, key=lambda f: f.get(
            "updated_at", ""), reverse=True):
            if isinstance(file, dict) and "display_name" in file:
                if file["display_name"] not in files_seen:
                    files_seen.add(file["display_name"])
                    files_to_download.append(file)
            else:
                logger.warning(f"Unexpected file format in folder {
                               folder_name} of course {course_name}: {file}")

        logger.debug(f"{course_name}'s folder {folder_name} has {
                     len(files_to_download)} files to download")
        async with trio.open_nursery() as nursery:
            for file in files_to_download:
                nursery.start_soon(
                    download_file, api, file, folder_name, course_name, progress_tracker, config)
    except CanvasAPIError as e:
        logger.error(f"Canvas API error while processing folder {
                     folder_name} in course {course_name}: {str(e)}")
    except Exception as e:
        logger.error(f"Error processing folder {
                     folder_name} in course {course_name}: {str(e)}")


async def process_course(api: CanvasAPI, course: CourseDict, progress_tracker: ProgressTracker, config: CanvasConfig) -> None:
    try:
        folders = await api.get_folder_list(str(course["id"]))
        num_files = 0
        for folder in folders:
            try:
                files = await api.get_files(folder["files_url"])
            except CanvasAPIError as e:
                logger.error(f"Canvas API error while processing course {
                             course['name']}, folder {folder['full_name']}: {str(e)}")
                continue

            if not isinstance(files, list):
                logger.error(f"Unexpected response format for files in course {
                             course['name']}. Response: {files}")
                continue

            try:
                num_files += len(set(file["display_name"] for file in files if isinstance(
                    file, dict) and "display_name" in file))
            except Exception as e:
                logger.error(f"Error processing files for course {
                             course['name']}: {str(e)}")
                logger.debug(f"Files data: {files}")
                continue

        logger.debug(f"{course['name']} has {num_files} files")
        progress_tracker.add_course_task(course["name"], num_files)

        async with trio.open_nursery() as nursery:
            for folder in folders:
                nursery.start_soon(
                    get_files_and_download,
                    api,
                    folder["files_url"],
                    folder["full_name"],
                    course["name"],
                    progress_tracker,
                    config,
                )
    except CanvasAPIError as e:
        logger.error(f"Canvas API error while processing course {
                     course['name']}: {str(e)}")
    except Exception as e:
        logger.error(f"Error processing course {course['name']}: {str(e)}")


async def run(config: CanvasConfig) -> None:
    if not config.base_url or not config.token or not config.base_dir:
        print("Missing configuration. Please run python canvas.py setup to get started.")
        return

    async with config.api as api:
        try:
            courses = await api.get_courses()
            courses_to_track = [course for course in courses if config.selections.get(
                str(course["id"]), False)]

            logger.debug("Courses to track: " +
                         ", ".join([course["name"] for course in courses_to_track]))

            print("Downloading files...\n")

            with Progress() as progress:
                progress_tracker = ProgressTracker(progress)

                async with trio.open_nursery() as nursery:
                    for course in courses_to_track:
                        nursery.start_soon(
                            process_course, api, course, progress_tracker, config)

            print("\nDownload process completed for all courses.")

            # Pretty print downloaded files
            if progress_tracker.downloaded_files:
                tree = Tree(
    "📁 Downloaded/Updated Files",
     guide_style="bold bright_blue")
                for course_name, files in progress_tracker.downloaded_files.items():
                    course_branch = tree.add(f"[bold green]🎓 {course_name}[/]")
                    folders = {}
                    for file_info in files:
                        folder_path = file_info['folder']
                        if folder_path not in folders:
                            folders[folder_path] = []
                        folders[folder_path].append(file_info['name'])

                    for folder_path, filenames in folders.items():
                        folder_branch = course_branch.add(
                            f"[cyan]📂 {folder_path}[/]",
                            guide_style="cyan"
                        )
                        for filename in filenames:
                            folder_branch.add(f"📄 [white]{filename}[/]")
                rprint(tree)
            else:
                rprint("[bold yellow]🌟 No new files were downloaded or updated.[/]")

        except CanvasAPIError as e:
            logger.error(f"Canvas API error: {str(e)}")
            print(
                f"An error occurred while communicating with Canvas: {str(e)}")


async def setup(config: CanvasConfig) -> None:
    default_url = config.base_url
    prompt = f"Enter your Canvas URL[{
        default_url}]: " if default_url else "Enter your Canvas URL: "
    base_url = prompt_for_input(
        prompt, validator=validate_url, default=default_url)
    config.update("base_url", clean_url(base_url))

    token_prompt = "Enter your OAuth token [previous token]: " if config.token else "Enter your OAuth token: "
    token = prompt_for_input(
        token_prompt, default=config.token) or config.token
    config.update("token", token)

    default_path = config.download_path or config.base_dir
    prompt = f"Enter path to download files [{default_path}]: "
    download_path = prompt_for_input(prompt, default=default_path)
    while not os.path.exists(download_path):
        print(f"Invalid path: {download_path}. ", end="")
        download_path = prompt_for_input(prompt, default=default_path)
    config.update("download_path", download_path)
    config.update("base_dir", download_path)

    cron_setup_choice = prompt_for_input(
        "Set up a cron job to run every 2 hours? (y/n) [n]: ", default="n").lower()
    script_path = os.path.abspath(__file__)
    setup_cron_job(script_path, add_job=cron_setup_choice == "y")

    async with config.api as api:
        courses = await api.get_courses()
        curses.wrapper(curses_select_courses, courses, config)

    script_name = os.path.basename(sys.argv[0])
    print(f"\nSetup complete! Please run \033[1mpython {
          script_name} run\033[0m to start tracking.")


async def main() -> None:
    parser= argparse.ArgumentParser()
    parser.add_argument("command", choices=["setup", "run"])
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    args= parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
    else:
        logger.setLevel(logging.WARNING)

    config= CanvasConfig()
    config.load()

    if args.command == "setup":
        await setup(config)
    elif args.command == "run":
        await run(config)

if __name__ == "__main__":
    trio.run(main)
