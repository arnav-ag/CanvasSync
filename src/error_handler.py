import logging

from exceptions import CanvasAPIError


class CanvasAPIErrorHandler:
    def __init__(self, logger):
        self.logger = logger

    async def handle_error(self, error: Exception):
        if isinstance(error, CanvasAPIError) and "Access forbidden" in str(
                error):
            self.logger.debug(f"Access forbidden: {str(error)}")
        else:
            self.logger.error(f"Error: {str(error)}")


error_handler = CanvasAPIErrorHandler(logging.getLogger("canvas_logger"))
