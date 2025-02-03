import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from api import CanvasAPI

CONFIG_FILE: str = os.path.join(os.path.dirname(__file__), ".config.json")


@dataclass
class CanvasConfig:
    base_url: str = ""
    base_dir: str = os.path.dirname(os.path.abspath(__file__))
    token: str = ""
    download_path: str = ""
    selections: Dict[str, bool] = field(default_factory=dict)
    _api: Optional[CanvasAPI] = None  # Changed to private

    @property
    def api(self) -> CanvasAPI:
        if self._api is None:
            self._api = CanvasAPI(self.base_url, self.token)
        return self._api

    def load(self) -> None:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as file:
                config_data = json.load(file)
                for key, value in config_data.items():
                    if key != "_api":  # Don't load the API from JSON
                        setattr(self, key, value)

    def save(self) -> None:
        with open(CONFIG_FILE, "w") as file:
            # Save everything except the API
            data = {k: v for k, v in self.__dict__.items() if k != "_api"}
            json.dump(data, file)

    def update(self, key: str, value: Any) -> None:
        setattr(self, key, value)
        if key in ["base_url", "token"]:
            self._api = CanvasAPI(self.base_url, self.token)
        self.save()
