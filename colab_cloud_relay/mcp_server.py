"""Standalone FastMCP server for Colab-Cloud-Relay (7 tools)."""

from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from .config import (
    auto_configure,
    copy_text_to_clipboard,
    get_colab_github_url,
    load_config,
    read_baidu_cookie_header,
)
from .queue_engine import ColabQueueManager

mcp = FastMCP(name="colab-cloud-relay")


def _qm() -> ColabQueueManager:
    cfg = load_config()
    if not cfg.drive_root or not Path(cfg.drive_root).is_dir():
        auto_configure()
        cfg = load_config()
    return ColabQueueManager(cfg.drive_root, relay_subdir=cfg.relay_subdir)


@mcp.tool()
def relay_auto_config(drive_root: str | None = None, baidu_auth_file: str | None = None) -> dict[str, Any]:
    """Auto-detect Google Drive, initialize ColabCloudRelay queue folders, deploy master .ipynb, and link Baidu auth."""
    return auto_configure(drive_root=drive_root, baidu_auth_file=baidu_auth_file)


@mcp.tool()
def relay_status() -> dict[str, Any]:
    """Check Google Drive mount, Colab cloud worker heartbeat, queue counts, and GitHub Colab URL."""
    cfg = load_config()
    qm = _qm()
    return {
        "drive_root": cfg.drive_root,
        "colab_github_url": get_colab_github_url(cfg.github_repo),
        "worker_heartbeat": qm.read_worker_heartbeat(),
        "queue_counts": qm.queue_counts(),
    }


@mcp.tool()
def relay_enqueue_baidu_to_gdrive(
    source_paths: list[str],
    target_dir: str = "百度网盘同步",
    title: str = "",
    daily_limit_gb: int = 700,
    workers: int = 4,
    parallel_ranges: int = 4,
) -> dict[str, Any]:
    """Enqueue a zero-VPN Baidu Netdisk SVIP10 -> Google Drive cloud transfer job for Colab worker."""
    return _qm().enqueue_job(
        "baidu_to_gdrive",
        source_paths=source_paths,
        target_dir=target_dir,
        title=title,
        daily_limit_gb=daily_limit_gb,
        workers=workers,
        parallel_ranges=parallel_ranges,
    )


@mcp.tool()
def relay_enqueue_gdrive_to_baidu(
    source_paths: list[str],
    target_dir: str = "/NotebookLM_同步",
    title: str = "",
) -> dict[str, Any]:
    """Enqueue a Google Drive -> Baidu Netdisk upload job for Colab worker."""
    return _qm().enqueue_job("gdrive_to_baidu", source_paths=source_paths, target_dir=target_dir, title=title)


@mcp.tool()
def relay_enqueue_url_to_gdrive(
    urls: list[str],
    target_dir: str = "Colab离线下载",
    title: str = "",
) -> dict[str, Any]:
    """Enqueue an offline HTTP/HTTPS URL -> Google Drive cloud download job inside Colab."""
    return _qm().enqueue_job("url_to_gdrive", urls=urls, target_dir=target_dir, title=title)


@mcp.tool()
def relay_jobs(job_id: str | None = None, limit: int = 30) -> dict[str, Any]:
    """Inspect a specific Colab Cloud Relay ticket or list all queued/running/done/failed tickets."""
    qm = _qm()
    if job_id:
        job = qm.get_job(job_id)
        if not job:
            raise ValueError(f"任务不存在: {job_id}")
        return job
    return {"jobs": qm.list_jobs(limit=limit), "counts": qm.queue_counts()}


@mcp.tool()
def relay_open_colab(copy_cookie: bool = True) -> dict[str, Any]:
    """Open the permanent Colab-Cloud-Relay GitHub notebook in the default browser and optionally copy BAIDU_COOKIE to clipboard."""
    cfg = load_config()
    if not cfg.drive_root:
        auto_configure()
        cfg = load_config()
    url = get_colab_github_url(cfg.github_repo)
    copied = False
    if copy_cookie:
        probe = read_baidu_cookie_header(cfg.baidu_auth_file or None)
        if probe["authenticated"]:
            copied = copy_text_to_clipboard(probe["cookie_header"])
    opened = bool(webbrowser.open_new_tab(url))
    return {"ok": True, "url": url, "browser_opened": opened, "cookie_copied_to_clipboard": copied}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
