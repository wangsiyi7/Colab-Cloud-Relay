"""Colab-Cloud-Relay: Pluggable Zero-VPN Cloud Drive & Transfer Node backed by Google Colab."""

from .config import (
    GITHUB_REPO_DEFAULT,
    RelayConfig,
    auto_configure,
    copy_text_to_clipboard,
    detect_google_drive_roots,
    discover_local_baidu_auth_files,
    get_colab_github_url,
    load_config,
    read_baidu_cookie_header,
    save_config,
)
from .fastapi_plugin import create_colab_relay_router
from .notebook_builder import build_master_worker_notebook, write_master_notebook
from .queue_engine import ColabQueueManager

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "GITHUB_REPO_DEFAULT",
    "RelayConfig",
    "ColabQueueManager",
    "auto_configure",
    "build_master_worker_notebook",
    "copy_text_to_clipboard",
    "create_colab_relay_router",
    "detect_google_drive_roots",
    "discover_local_baidu_auth_files",
    "get_colab_github_url",
    "load_config",
    "read_baidu_cookie_header",
    "save_config",
    "write_master_notebook",
]
