"""Standalone CLI and Web Server for Colab-Cloud-Relay."""

from __future__ import annotations

import argparse
import json
import webbrowser
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .config import (
    auto_configure,
    copy_text_to_clipboard,
    get_colab_github_url,
    load_config,
    read_baidu_cookie_header,
)
from .fastapi_plugin import create_colab_relay_router
from .queue_engine import ColabQueueManager


def create_standalone_app() -> FastAPI:
    app = FastAPI(title="Colab-Cloud-Relay Control Center", version="0.1.0")
    app.include_router(create_colab_relay_router(prefix="/api/colab-relay"))

    ui_path = Path(__file__).with_name("web_ui.html")

    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse(ui_path.read_text(encoding="utf-8"))

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "colab-cloud-relay"}

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="colab-relay", description="Colab-Cloud-Relay CLI & Standalone Control Center")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_setup = sub.add_parser("setup", help="Auto-detect Google Drive, deploy master .ipynb, optionally copy cookie & open Colab")
    p_setup.add_argument("--drive-root", default=None)
    p_setup.add_argument("--baidu-auth-file", default=None)
    p_setup.add_argument("--copy-cookie", action="store_true")
    p_setup.add_argument("--open", action="store_true")

    sub.add_parser("status", help="Show Google Drive mount, worker heartbeat, and queue counts")

    p_enq = sub.add_parser("enqueue", help="Enqueue a cloud transfer job")
    p_enq.add_argument("--kind", default="baidu_to_gdrive", choices=["baidu_to_gdrive", "gdrive_to_baidu", "url_to_gdrive", "gdrive_organize"])
    p_enq.add_argument("--src", action="append", default=[])
    p_enq.add_argument("--url", action="append", default=[])
    p_enq.add_argument("--target", default="")
    p_enq.add_argument("--title", default="")

    sub.add_parser("jobs", help="List cloud relay tickets")

    p_serve = sub.add_parser("serve", help="Run standalone Colab-Cloud-Relay Web UI & API server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8775)
    p_serve.add_argument("--open", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "setup":
        res = auto_configure(drive_root=args.drive_root, baidu_auth_file=args.baidu_auth_file)
        if args.copy_cookie:
            probe = read_baidu_cookie_header(res.get("baidu_auth_file") or None)
            res["cookie_copied_to_clipboard"] = copy_text_to_clipboard(probe["cookie_header"]) if probe["authenticated"] else False
        if args.open:
            res["browser_opened"] = bool(webbrowser.open_new_tab(res["colab_github_url"]))
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    cfg = load_config()
    if args.cmd == "status":
        qm = ColabQueueManager(cfg.drive_root, relay_subdir=cfg.relay_subdir) if cfg.drive_root else None
        out = {
            "configured": bool(cfg.drive_root),
            "drive_root": cfg.drive_root,
            "colab_github_url": get_colab_github_url(cfg.github_repo),
            "worker_heartbeat": qm.read_worker_heartbeat() if qm and Path(cfg.drive_root).is_dir() else {"online": False},
            "queue_counts": qm.queue_counts() if qm and Path(cfg.drive_root).is_dir() else {},
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "enqueue":
        if not cfg.drive_root:
            auto_configure()
            cfg = load_config()
        qm = ColabQueueManager(cfg.drive_root, relay_subdir=cfg.relay_subdir)
        ticket = qm.enqueue_job(args.kind, source_paths=args.src, urls=args.url, target_dir=args.target, title=args.title)
        print(json.dumps(ticket, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "jobs":
        if not cfg.drive_root:
            print("Not configured yet. Run: colab-relay setup")
            return 1
        qm = ColabQueueManager(cfg.drive_root, relay_subdir=cfg.relay_subdir)
        print(json.dumps(qm.list_jobs(), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "serve":
        import uvicorn

        auto_configure()
        if args.open:
            webbrowser.open_new_tab(f"http://{args.host}:{args.port}/")
        uvicorn.run(create_standalone_app(), host=args.host, port=args.port)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
