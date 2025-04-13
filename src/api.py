import logging
from typing import Any, Optional

import httpx
import trio

from exceptions import CanvasAPIError
from models import CourseDict, FileDict

logger = logging.getLogger("canvas_logger")


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
            headers={"Authorization": f"Bearer {self.token}"},
        )

    async def close_client(self):
        if self.client:
            await self.client.aclose()
            self.client = None

    async def _request(
        self,
        method: str,
        endpoint: str,
        **kwargs,
    ) -> Any:
        if not self.client:
            await self.create_client()

        assert self.client is not None

        url = f"{self.base_url}/api/v1/{endpoint}"
        params = kwargs.get("params", {})
        params["per_page"] = self.page_limit

        max_retries = 3
        retry_delay = 1

        for attempt in range(max_retries):
            try:
                self.logger.debug(
                    f"Trying to get URL: {url=} {method=} {params=} {kwargs=}",
                )
                response = await self.client.request(
                    method,
                    url,
                    params=params,
                    **kwargs,
                )
                if response.status_code == 403:
                    self.logger.debug(f"Access forbidden. Data: {response.text}")
                    raise CanvasAPIError(
                        "Access forbidden. Check your API token and permissions.",
                    )
                return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:  # Too Many Requests
                    retry_after = int(
                        e.response.headers.get("Retry-After", retry_delay),
                    )
                    self.logger.warning(
                        f"Rate limited. Retrying after {retry_after} seconds.",
                    )
                    await trio.sleep(retry_after)
                elif attempt == max_retries - 1:
                    raise CanvasAPIError(
                        f"HTTP error {e.response.status_code}: {e!s}",
                    )
                else:
                    await trio.sleep(retry_delay * (2**attempt))
            except httpx.RequestError as e:
                raise CanvasAPIError(f"Network error: {e!s}")

        raise CanvasAPIError("Max retries reached. Unable to complete the request.")

    async def get_courses(self) -> list[CourseDict]:
        courses = await self._request("GET", "courses")
        return [course for course in courses if "name" in course and "id" in course]

    async def get_folder_list(self, course_id: str) -> list[dict[str, Any]]:
        return await self._request("GET", f"courses/{course_id}/folders")

    async def get_files(self, files_url: str) -> list[FileDict]:
        relative_path = files_url.split("/api/v1/")[-1]
        return await self._request("GET", relative_path)

    async def download_file(self, file_url: str) -> bytes:
        try:
            async with httpx.AsyncClient(follow_redirects=False) as client:
                response = await client.get(file_url)
                while response.status_code == 302:
                    # we want to follow the redirects and update the cookies at each step
                    # to handle the authentication
                    redirect_url = response.headers["Location"]
                    response = await client.get(redirect_url)
                    # update the cookies for the next request
                    client.cookies.update(response.cookies)

                response.raise_for_status()
                return response.content
        except httpx.HTTPStatusError as e:
            raise CanvasAPIError(
                f"HTTP error {e.response.status_code} while downloading file: {e!s}",
            )
        except httpx.RequestError as e:
            raise CanvasAPIError(f"Network error while downloading file: {e!s}")
