import curses
import datetime
import logging
import os
import sys
from typing import List

import trio
from rich import print as rprint
from rich.progress import Progress
from rich.tree import Tree

from config import CanvasConfig
from error_handler import error_handler
from exceptions import CanvasAPIError
from models import CourseDict, ProgressTracker
from utils import (
    change_last_modified,
    clean_url,
    escape_path,
    is_changed_since,
    is_edited_since,
    prompt_for_input,
    setup_cron_job,
    validate_url,
)

logger = logging.getLogger("canvas_logger")


def curses_select_courses(
    screen: curses.window, courses: List[CourseDict], config: CanvasConfig
) -> None:
    current_row = 0
    selections = {
        course["id"]: config.selections.get(str(course["id"]), False)
        for course in courses
    }

    def print_menu(row: int) -> None:
        screen.clear()
        screen.addstr("Select courses to track:\n\n")

        for idx, course in enumerate(courses):
            selected_indicator = "[✔]" if selections.get(course["id"], False) else "[ ]"
            if idx == row:
                screen.addstr(
                    f"{selected_indicator} {
                        course['name']}\n",
                    curses.A_REVERSE,
                )
            else:
                screen.addstr(f"{selected_indicator} {course['name']}\n")

        screen.addstr(
            f"\n{len([course for course in courses if selections.get(
                course['id'], False)])} courses selected\n"
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

    config.update("selections", {str(k): v for k, v in selections.items()})


async def download_file(
    api,
    file,
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
        config.base_dir, clean_course_name, clean_folder_name, display_name
    )

    updated_at_str = file.get("updated_at", "0000-00-00T00:00:00Z")
    updated_dt = datetime.datetime.strptime(updated_at_str, "%Y-%m-%dT%H:%M:%SZ")

    if is_edited_since(full_file_path, updated_dt) or not is_changed_since(
        full_file_path, updated_dt
    ):
        progress_tracker.advance_course_task(course_name)
        logger.debug(f"Skipped file from {course_name}")
        return

    try:
        file_content = await api.download_file(file_url)
        with open(full_file_path, "wb") as f:
            f.write(file_content)

        progress_tracker.downloaded_files.setdefault(course_name, []).append(
            {"folder": folder_name, "name": display_name}
        )

        logger.debug(f"Downloaded {full_file_path}")
        change_last_modified(full_file_path, updated_dt)
        progress_tracker.advance_course_task(course_name)
    except CanvasAPIError as e:
        await error_handler.handle_error(e)
    except Exception as e:
        logger.error(
            f"Error downloading file {display_name} from {
                course_name}: {str(e)}"
        )


async def get_files_and_download(
    api,
    files_url: str,
    folder_name: str,
    course_name: str,
    progress_tracker: ProgressTracker,
    config: CanvasConfig,
) -> None:
    try:
        files = await api.get_files(files_url)

        if not isinstance(files, list):
            logger.error(
                f"Unexpected response format for files in folder {
                    folder_name} of course {course_name}. Response: {files}"
            )
            return

        files_seen: set[str] = set()
        files_to_download = []
        for file in sorted(files, key=lambda f: f.get("updated_at", ""), reverse=True):
            if isinstance(file, dict) and "display_name" in file:
                if file["display_name"] not in files_seen:
                    files_seen.add(file["display_name"])
                    files_to_download.append(file)
            else:
                logger.warning(
                    f"Unexpected file format in folder {
                        folder_name} of course {course_name}: {file}"
                )

        logger.debug(
            f"{course_name}'s folder {folder_name} has {
                len(files_to_download)} files to download"
        )
        async with trio.open_nursery() as nursery:
            for file in files_to_download:
                nursery.start_soon(
                    download_file,
                    api,
                    file,
                    folder_name,
                    course_name,
                    progress_tracker,
                    config,
                )
    except CanvasAPIError as e:
        await error_handler.handle_error(e)
    except Exception as e:
        logger.error(
            f"Error processing folder {
                folder_name} in course {course_name}: {str(e)}"
        )


async def process_course(
    api, course: CourseDict, progress_tracker: ProgressTracker, config: CanvasConfig
) -> None:
    try:
        folders = await api.get_folder_list(str(course["id"]))
        num_files = 0
        for folder in folders:
            try:
                files = await api.get_files(folder["files_url"])
            except CanvasAPIError as e:
                await error_handler.handle_error(e)
                continue

            if not isinstance(files, list):
                logger.error(
                    f"Unexpected response format for files in course {
                        course['name']}. Response: {files}"
                )
                continue

            try:
                num_files += len(
                    set(
                        file["display_name"]
                        for file in files
                        if isinstance(file, dict) and "display_name" in file
                    )
                )
            except Exception as e:
                logger.error(
                    f"Error processing files for course {
                        course['name']}: {str(e)}"
                )
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
        await error_handler.handle_error(e)
    except Exception as e:
        logger.error(f"Error processing course {course['name']}: {str(e)}")


async def run(config: CanvasConfig) -> None:
    if not (
        config.base_url
        and config.token
        and config.base_dir
        and config.download_path
        and config.selections
        and config.api
    ):
        print(
            "Missing configuration. Please run python canvas.py setup to get started."
        )
        return

    async with config.api as api:
        try:
            courses = await api.get_courses()
            courses_to_track = [
                course
                for course in courses
                if config.selections.get(str(course["id"]), False)
            ]

            logger.debug(
                "Courses to track: "
                + ", ".join([course["name"] for course in courses_to_track])
            )

            print("Downloading files...\n")

            with Progress() as progress:
                progress_tracker = ProgressTracker(progress)

                async with trio.open_nursery() as nursery:
                    for course in courses_to_track:
                        nursery.start_soon(
                            process_course, api, course, progress_tracker, config
                        )

            print("\nDownload process completed for all courses.")

            # Pretty print downloaded files
            if progress_tracker.downloaded_files:
                tree = Tree(
                    "📁 Downloaded/Updated Files", guide_style="bold bright_blue"
                )
                for course_name, files in progress_tracker.downloaded_files.items():
                    course_branch = tree.add(f"[bold green]🎓 {course_name}[/]")
                    folders: dict[str, list[str]] = {}
                    for file_info in files:
                        folder_path = file_info["folder"]
                        if folder_path not in folders:
                            folders[folder_path] = []
                        folders[folder_path].append(file_info["name"])

                    for folder_path, filenames in folders.items():
                        folder_branch = course_branch.add(
                            f"[cyan]📂 {folder_path}[/]", guide_style="cyan"
                        )
                        for filename in filenames:
                            folder_branch.add(f"📄 [white]{filename}[/]")
                rprint(tree)
            else:
                rprint("[bold yellow]🌟 No new files were downloaded or updated.[/]")

        except CanvasAPIError as e:
            await error_handler.handle_error(e)


async def setup(config: CanvasConfig) -> None:
    default_url = config.base_url
    prompt = (
        f"Enter your Canvas URL[{default_url}]: "
        if default_url
        else "Enter your Canvas URL: "
    )
    base_url = prompt_for_input(prompt, validator=validate_url, default=default_url)
    while not base_url:
        print("Invalid URL. Please try again.")
        base_url = prompt_for_input(prompt, validator=validate_url, default=default_url)

    config.update("base_url", clean_url(base_url))

    token_prompt = (
        "Enter your OAuth token [previous token]: "
        if config.token
        else "Enter your OAuth token: "
    )
    token = prompt_for_input(token_prompt, default=config.token) or config.token
    config.update("token", token)

    default_path = config.download_path or config.base_dir
    prompt = f"Enter path to download files [{default_path}]: "
    download_path = prompt_for_input(prompt, default=default_path)
    while not download_path or not os.path.exists(download_path):
        print(f"Invalid path: {download_path}. ", end="")
        download_path = prompt_for_input(prompt, default=default_path)
    config.update("download_path", download_path)
    config.update("base_dir", download_path)

    cron_setup_choice = prompt_for_input(
        "Set up a cron job to run every 2 hours? (y/n) [n]: ", default="n"
    ).lower()
    script_path = os.path.join(os.path.dirname(__file__), "canvas.py")
    setup_cron_job(script_path, add_job=cron_setup_choice == "y")

    async with config.api as api:
        courses = await api.get_courses()
        curses.wrapper(curses_select_courses, courses, config)

    script_name = os.path.basename(sys.argv[0])
    print(
        f"\nSetup complete! Please run \033[1mpython {
            script_name} run\033[0m to start tracking."
    )
