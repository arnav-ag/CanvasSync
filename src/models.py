from dataclasses import dataclass, field
from typing import Dict, List, TypedDict

from rich.progress import Progress, TaskID


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


@dataclass
class ProgressTracker:
    progress: Progress
    task_ids: Dict[str, TaskID] = field(default_factory=dict)
    downloaded_files: Dict[str, List[Dict[str, str]]
                           ] = field(default_factory=dict)

    def add_course_task(self, course_name: str, total: int) -> TaskID:
        task_id = self.progress.add_task(
            f"Downloading files for {course_name}", total=total)
        self.task_ids[course_name] = task_id
        return task_id

    def advance_course_task(self, course_name: str) -> None:
        task_id = self.task_ids.get(course_name)
        if task_id is not None:
            self.progress.update(task_id, advance=1)
