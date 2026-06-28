"""配置层：pydantic-settings 集中加载 env / 只读挂载文件。

提供:
  - settings.py : Settings(BaseSettings) + get_settings() + read_secret()
  - scrub.py    : scrub() + setup_logging()
"""

from panel.config.scrub import scrub, setup_logging
from panel.config.settings import Settings, get_settings, read_secret

__all__ = [
    "Settings",
    "get_settings",
    "read_secret",
    "scrub",
    "setup_logging",
]
