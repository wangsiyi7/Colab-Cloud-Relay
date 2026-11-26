"""Mountable FastAPI plugin router for Colab-Cloud-Relay."""

from __future__ import annotations

import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import config as _cfg
from .config import (
    MASTER_NOTEBOOK_FILENAME,
    RelayConfig,
    auto_configure,
    get_colab_github_url,
    load_config,
    read_baidu_cookie_header,
    save_config,
)
from .notebook_builder import write_master_notebook
from .queue_engine import ColabQueueManager


class AutoConfigureRequest(BaseModel):
    drive_root: str | None = None
    baidu_auth_file: str | None = None
    github_repo: str | None = None
    relay_subdir: str | None = None


class EnqueueRelayJobRequest(BaseModel):
    kind: str = Field(
        default="baidu_to_gdrive",
        pattern="^(baidu_to_gdrive|gdrive_to_baidu|url_to_gdrive|gdrive_organize)$",
    )
    source_paths: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)
    target_dir: str = ""
    title: str = ""
    preserve_tree: bool = True
    skip_existing: bool = True
    workers: int = Field(default=4, ge=1, le=8)
    parallel_ranges: int = Field(default=4, ge=1, le=8)
    daily_limit_gb: int = Field(default=700, ge=0, le=5000)
    exclude_patterns: list[str] = Field(default_factory=list)
    receipt_drive_rel_path: str = ""
    auto_disconnect: bool = True


class CopyCookieRequest(BaseModel):
    copy_to_clipboard: bool = True


class OpenColabRequest(BaseModel):
    copy_cookie_to_clipboard: bool = True
    use_github_url: bool | None = None


class RelayConfigUpdateRequest(BaseModel):
    colab_drive_url: str | None = None
    drive_root: str | None = None
    default_gdrive_subdir: str | None = None
    default_baidu_dir: str | None = None
    default_workers: int | None = Field(default=None, ge=1, le=8)
    default_parallel_ranges: int | None = Field(default=None, ge=1, le=8)
    default_daily_limit_gb: int | None = Field(default=None, ge=0, le=5000)


def _ensure_qm(cfg: RelayConfig) -> ColabQueueManager:
    if not cfg.drive_root or not Path(cfg.drive_root).exists():
        res = auto_configure()
        cfg = load_config()
        if not res["drive_root"]:
            raise HTTPException(status_code=400, detail="未检测到 Google Drive 目录，请先调用 auto-configure 指定 drive_root")
    return ColabQueueManager(cfg.drive_root, relay_subdir=cfg.relay_subdir)


def create_colab_relay_router(
    prefix: str = "/api/colab-relay",
    *,
    config_loader: Callable[[], RelayConfig] = load_config,
) -> APIRouter:
    """Create a pluggable FastAPI router that mounts into any host app in one line."""
    router = APIRouter(prefix=prefix, tags=["colab-cloud-relay"])

    @router.get("/status")
    async def relay_status() -> dict[str, Any]:
        cfg = config_loader()
        drive_ok = bool(cfg.drive_root and Path(cfg.drive_root).is_dir())
        qm = ColabQueueManager(cfg.drive_root, relay_subdir=cfg.relay_subdir) if cfg.drive_root else None
        hb = qm.read_worker_heartbeat() if qm and drive_ok else {"online": False, "status": "unconfigured"}
        counts = qm.queue_counts() if qm and drive_ok else {"queue": 0, "running": 0, "done": 0, "failed": 0}
        master_nb = str(qm.dirs()["notebooks"] / MASTER_NOTEBOOK_FILENAME) if qm else ""
        auth_probe = read_baidu_cookie_header(cfg.baidu_auth_file or None)
        gh_url = get_colab_github_url(cfg.github_repo)
        drive_url = (cfg.colab_drive_url or "").strip()
        return {
            "configured": bool(cfg.drive_root),
            "drive_root": cfg.drive_root,
            "drive_mounted": drive_ok,
            "relay_subdir": cfg.relay_subdir,
            "github_repo": cfg.github_repo,
            "colab_github_url": gh_url,
            "colab_drive_url": drive_url,
            "preferred_colab_url": drive_url or gh_url,
            "master_notebook_drive_path": master_nb,
            "master_notebook_exists": bool(master_nb and Path(master_nb).is_file()),
            "baidu_ready": bool(auth_probe["authenticated"]),
            "baidu_has_stoken": bool(auth_probe["has_stoken"]),
            "baidu_auth_file": auth_probe.get("auth_file") or cfg.baidu_auth_file,
            "worker_heartbeat": hb,
            "queue_counts": counts,
            "defaults": {
                "default_gdrive_subdir": cfg.default_gdrive_subdir,
                "default_baidu_dir": cfg.default_baidu_dir,
                "workers": cfg.default_workers,
                "parallel_ranges": cfg.default_parallel_ranges,
                "daily_limit_gb": cfg.default_daily_limit_gb,
                "secret_configured": cfg.secret_configured,
            },
        }

    @router.post("/auto-configure")
    async def relay_auto_configure(body: AutoConfigureRequest | None = None) -> dict[str, Any]:
        req = body or AutoConfigureRequest()
        return auto_configure(
            drive_root=req.drive_root,
            baidu_auth_file=req.baidu_auth_file,
            github_repo=req.github_repo,
            relay_subdir=req.relay_subdir,
        )

    @router.put("/config")
    async def relay_update_config(body: RelayConfigUpdateRequest) -> dict[str, Any]:
        cfg = config_loader()
        if body.colab_drive_url is not None:
            raw_url = body.colab_drive_url.strip()
            if raw_url and not raw_url.startswith("https://colab.research.google.com/"):
                raise HTTPException(status_code=400, detail="colab_drive_url 必须以 https://colab.research.google.com/ 开头")
            cfg.colab_drive_url = raw_url
        if body.drive_root is not None and body.drive_root.strip():
            cfg.drive_root = body.drive_root.strip()
        if body.default_gdrive_subdir is not None and body.default_gdrive_subdir.strip():
            cfg.default_gdrive_subdir = body.default_gdrive_subdir.strip()
        if body.default_baidu_dir is not None and body.default_baidu_dir.strip():
            cfg.default_baidu_dir = body.default_baidu_dir.strip()
        if body.default_workers is not None:
            cfg.default_workers = body.default_workers
        if body.default_parallel_ranges is not None:
            cfg.default_parallel_ranges = body.default_parallel_ranges
        if body.default_daily_limit_gb is not None:
            cfg.default_daily_limit_gb = body.default_daily_limit_gb
        save_config(cfg)
        gh_url = get_colab_github_url(cfg.github_repo)
        return {
            "ok": True,
            "config": cfg.to_dict(),
            "preferred_colab_url": cfg.colab_drive_url or gh_url,
        }

    @router.post("/jobs/enqueue")
    async def relay_enqueue_job(body: EnqueueRelayJobRequest) -> dict[str, Any]:
        cfg = config_loader()
        qm = _ensure_qm(cfg)
        try:
            ticket = qm.enqueue_job(
                body.kind,
                source_paths=body.source_paths,
                urls=body.urls,
                target_dir=body.target_dir,
                title=body.title,
                preserve_tree=body.preserve_tree,
                skip_existing=body.skip_existing,
                workers=body.workers,
                parallel_ranges=body.parallel_ranges,
                daily_limit_gb=body.daily_limit_gb,
                exclude_patterns=body.exclude_patterns,
                receipt_drive_rel_path=body.receipt_drive_rel_path,
                auto_disconnect=body.auto_disconnect,
            )
            return {"ok": True, "ticket": ticket}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/jobs")
    async def relay_list_jobs(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
        cfg = config_loader()
        if not cfg.drive_root or not Path(cfg.drive_root).is_dir():
            return {"jobs": [], "counts": {"queue": 0, "running": 0, "done": 0, "failed": 0}}
        qm = ColabQueueManager(cfg.drive_root, relay_subdir=cfg.relay_subdir)
        return {"jobs": qm.list_jobs(limit=limit), "counts": qm.queue_counts()}

    @router.get("/jobs/{job_id}")
    async def relay_get_job(job_id: str) -> dict[str, Any]:
        cfg = config_loader()
        qm = _ensure_qm(cfg)
        try:
            item = qm.get_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not item:
            raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
        return item

    @router.post("/jobs/{job_id}/retry")
    async def relay_retry_job(job_id: str) -> dict[str, Any]:
        cfg = config_loader()
        qm = _ensure_qm(cfg)
        try:
            ticket = qm.retry_failed_job(job_id)
            return {"ok": True, "ticket": ticket}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/jobs/{job_id}")
    async def relay_delete_job(job_id: str) -> dict[str, Any]:
        cfg = config_loader()
        qm = _ensure_qm(cfg)
        try:
            return qm.delete_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/master-notebook/download")
    async def relay_download_master_notebook() -> FileResponse:
        cfg = config_loader()
        if cfg.drive_root and Path(cfg.drive_root).is_dir():
            qm = ColabQueueManager(cfg.drive_root, relay_subdir=cfg.relay_subdir)
            nb_path = qm.dirs()["notebooks"] / MASTER_NOTEBOOK_FILENAME
            if not nb_path.is_file():
                write_master_notebook(nb_path, github_repo=cfg.github_repo)
            return FileResponse(path=nb_path, media_type="application/x-ipynb+json", filename=MASTER_NOTEBOOK_FILENAME)
        raise HTTPException(status_code=404, detail="请先运行 auto-configure 生成主节点脚本")

    @router.post("/copy-cookie")
    async def relay_copy_cookie(request: Request, body: CopyCookieRequest | None = None) -> dict[str, Any]:
        client_host = (request.client.host if request.client else "") or ""
        if client_host not in ("127.0.0.1", "::1", "localhost", "testclient"):
            raise HTTPException(status_code=403, detail="出于安全限制，仅允许本机回环请求读取 Baidu Cookie")
        cfg = config_loader()
        probe = read_baidu_cookie_header(cfg.baidu_auth_file or None)
        if not probe["authenticated"]:
            raise HTTPException(status_code=400, detail="本地未检测到有效百度网盘 Cookie，请先登录百度网盘")
        req = body or CopyCookieRequest()
        copied = _cfg.copy_text_to_clipboard(probe["cookie_header"]) if req.copy_to_clipboard else False
        return {
            "ok": True,
            "secret_name": "BAIDU_COOKIE",
            "mode": probe["mode"],
            "has_stoken": probe["has_stoken"],
            "copied_to_clipboard": copied,
            "cookie_header": probe["cookie_header"],
        }

    @router.post("/open-colab")
    async def relay_open_colab(request: Request, body: OpenColabRequest | None = None) -> dict[str, Any]:
        client_host = (request.client.host if request.client else "") or ""
        if client_host not in ("127.0.0.1", "::1", "localhost", "testclient"):
            raise HTTPException(status_code=403, detail="仅允许本机回环请求拉起外部浏览器")
        cfg = config_loader()
        if not cfg.drive_root:
            auto_configure()
            cfg = config_loader()
        req = body or OpenColabRequest()
        if req.use_github_url is True:
            url = get_colab_github_url(cfg.github_repo)
        elif req.use_github_url is False:
            url = (cfg.colab_drive_url or "").strip() or "https://colab.research.google.com/"
        else:
            url = (cfg.colab_drive_url or "").strip() or get_colab_github_url(cfg.github_repo)
        copied = False
        if req.copy_cookie_to_clipboard:
            probe = read_baidu_cookie_header(cfg.baidu_auth_file or None)
            if probe["authenticated"]:
                copied = _cfg.copy_text_to_clipboard(probe["cookie_header"])
                if copied:
                    cfg.secret_configured = True
                    save_config(cfg)
        opened = False
        if client_host != "testclient":
            try:
                opened = bool(webbrowser.open_new_tab(url))
            except Exception:  # noqa: BLE001
                opened = False
        return {
            "ok": True,
            "url": url,
            "browser_opened": opened,
            "cookie_copied_to_clipboard": copied,
            "secret_name": "BAIDU_COOKIE",
        }

    return router
