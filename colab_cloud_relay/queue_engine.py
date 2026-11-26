"""Google Drive JSON Queue Manager for Colab-Cloud-Relay."""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DEFAULT_RELAY_SUBDIR

VALID_JOB_KINDS = ("baidu_to_gdrive", "gdrive_to_baidu", "url_to_gdrive", "gdrive_organize")
_SAFE_ID_RE = re.compile(r"^[\w\-]{3,64}$")


class ColabQueueManager:
    """Manages `queue/`, `running/`, `done/`, `failed/`, `logs/`, and `notebooks/` under Google Drive."""

    def __init__(self, drive_root: str | Path, relay_subdir: str = DEFAULT_RELAY_SUBDIR) -> None:
        self.drive_root = Path(drive_root)
        self.relay_subdir = (relay_subdir or DEFAULT_RELAY_SUBDIR).strip().strip("/\\") or DEFAULT_RELAY_SUBDIR
        self.root = self.drive_root / self.relay_subdir

    def dirs(self) -> dict[str, Path]:
        return {
            "root": self.root,
            "queue": self.root / "queue",
            "running": self.root / "running",
            "done": self.root / "done",
            "failed": self.root / "failed",
            "logs": self.root / "logs",
            "notebooks": self.root / "notebooks",
        }

    def ensure_dirs(self) -> dict[str, Path]:
        d = self.dirs()
        for p in d.values():
            p.mkdir(parents=True, exist_ok=True)
        return d

    def enqueue_job(
        self,
        kind: str,
        *,
        source_paths: list[str] | None = None,
        urls: list[str] | None = None,
        target_dir: str = "",
        title: str = "",
        preserve_tree: bool = True,
        skip_existing: bool = True,
        workers: int = 4,
        parallel_ranges: int = 4,
        daily_limit_gb: int = 700,
        exclude_patterns: list[str] | None = None,
        receipt_drive_rel_path: str = "",
    ) -> dict[str, Any]:
        if kind not in VALID_JOB_KINDS:
            raise ValueError(f"不支持的任务类型 {kind!r}，可选: {', '.join(VALID_JOB_KINDS)}")

        clean_sources = [str(s).strip() for s in (source_paths or []) if str(s).strip()]
        clean_urls = [str(u).strip() for u in (urls or []) if str(u).strip()]
        if kind in ("baidu_to_gdrive", "gdrive_to_baidu") and not clean_sources:
            raise ValueError("请至少提供一个待传输源路径 (source_paths)")
        if kind == "url_to_gdrive" and not clean_urls:
            raise ValueError("请至少提供一个 HTTP/HTTPS 下载链接 (urls)")

        d = self.ensure_dirs()
        job_id = "CR-" + uuid.uuid4().hex[:10]
        default_target = {
            "baidu_to_gdrive": "百度网盘同步",
            "gdrive_to_baidu": "/NotebookLM_同步",
            "url_to_gdrive": "Colab离线下载",
            "gdrive_organize": "",
        }[kind]
        resolved_target = (target_dir or "").strip() or default_target

        auto_title = (title or "").strip()
        if not auto_title:
            first_ref = (clean_sources or clean_urls or [resolved_target or kind])[0]
            auto_title = first_ref.rstrip("/\\").split("/")[-1].split("\\")[-1] or kind

        ticket = {
            "job_id": job_id,
            "kind": kind,
            "title": auto_title[:120],
            "source_paths": clean_sources,
            "urls": clean_urls,
            "target_dir": resolved_target,
            "preserve_tree": bool(preserve_tree),
            "skip_existing": bool(skip_existing),
            "workers": max(1, min(int(workers), 8)),
            "parallel_ranges": max(1, min(int(parallel_ranges), 8)),
            "daily_limit_gb": max(0, int(daily_limit_gb)),
            "exclude_patterns": [str(p).strip() for p in (exclude_patterns or []) if str(p).strip()],
            "receipt_drive_rel_path": (receipt_drive_rel_path or "").strip().lstrip("/\\"),
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_ts": time.time(),
        }
        target = d["queue"] / f"{job_id}.json"
        tmp = d["queue"] / f".{job_id}.tmp"
        tmp.write_text(json.dumps(ticket, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
        ticket["ticket_path"] = str(target)
        return ticket

    def read_worker_heartbeat(self, *, stale_after_sec: int = 120) -> dict[str, Any]:
        hb_file = self.root / "worker_heartbeat.json"
        if not hb_file.is_file():
            return {"online": False, "status": "offline", "age_sec": None, "heartbeat_file": str(hb_file)}
        try:
            data = json.loads(hb_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("invalid heartbeat")
            ts_epoch = data.get("ts_epoch")
            age = max(0.0, time.time() - float(ts_epoch)) if ts_epoch is not None else max(0.0, time.time() - hb_file.stat().st_mtime)
            online = age <= stale_after_sec and str(data.get("status") or "") not in ("stopped", "offline")
            return {
                **data,
                "online": online,
                "age_sec": round(age, 1),
                "heartbeat_file": str(hb_file),
            }
        except Exception as exc:  # noqa: BLE001
            return {"online": False, "status": "error", "error": str(exc), "heartbeat_file": str(hb_file)}

    def _read_ticket(self, p: Path, folder_state: str) -> dict[str, Any] | None:
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return None
        except Exception:  # noqa: BLE001
            return None
        raw.setdefault("job_id", p.stem)
        raw.setdefault("kind", "baidu_to_gdrive")
        raw.setdefault("created_ts", p.stat().st_mtime)
        if not raw.get("status"):
            raw["status"] = folder_state
        raw["folder_state"] = folder_state
        raw["ticket_path"] = str(p)
        return raw

    def list_jobs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        d = self.dirs()
        jobs: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for state in ("running", "queue", "done", "failed"):
            folder = d[state]
            if not folder.is_dir():
                continue
            for p in folder.glob("*.json"):
                if p.name.startswith("."):
                    continue
                item = self._read_ticket(p, state)
                if item and item["job_id"] not in seen_ids:
                    seen_ids.add(item["job_id"])
                    jobs.append(item)
        jobs.sort(key=lambda x: float(x.get("created_ts") or 0.0), reverse=True)
        return jobs[: max(1, min(int(limit), 200))]

    def queue_counts(self) -> dict[str, int]:
        d = self.dirs()
        counts: dict[str, int] = {}
        for state in ("queue", "running", "done", "failed"):
            folder = d[state]
            counts[state] = len([p for p in folder.glob("*.json") if not p.name.startswith(".")]) if folder.is_dir() else 0
        return counts

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        clean = (job_id or "").strip()
        if not _SAFE_ID_RE.fullmatch(clean):
            raise ValueError(f"非法 job_id: {job_id!r}")
        d = self.dirs()
        for state in ("running", "queue", "done", "failed"):
            p = (d[state] / f"{clean}.json").resolve()
            if p.is_relative_to(d[state].resolve()) and p.is_file():
                return self._read_ticket(p, state)
        return None

    def retry_failed_job(self, job_id: str) -> dict[str, Any]:
        existing = self.get_job(job_id)
        if not existing:
            raise FileNotFoundError(f"任务不存在: {job_id}")
        return self.enqueue_job(
            str(existing.get("kind") or "baidu_to_gdrive"),
            source_paths=existing.get("source_paths") or [],
            urls=existing.get("urls") or [],
            target_dir=str(existing.get("target_dir") or ""),
            title=str(existing.get("title") or ""),
            preserve_tree=bool(existing.get("preserve_tree", True)),
            skip_existing=bool(existing.get("skip_existing", True)),
            workers=int(existing.get("workers") or 4),
            parallel_ranges=int(existing.get("parallel_ranges") or 4),
            daily_limit_gb=int(existing.get("daily_limit_gb") if existing.get("daily_limit_gb") is not None else 700),
            exclude_patterns=existing.get("exclude_patterns") or [],
            receipt_drive_rel_path=str(existing.get("receipt_drive_rel_path") or ""),
        )

    def delete_job(self, job_id: str) -> dict[str, Any]:
        clean = (job_id or "").strip()
        if not _SAFE_ID_RE.fullmatch(clean):
            raise ValueError(f"非法 job_id: {job_id!r}")
        d = self.dirs()
        if (d["running"] / f"{clean}.json").is_file():
            raise RuntimeError("该任务正在 Colab 中运行，无法直接从队列删除")
        for state in ("queue", "done", "failed"):
            p = (d[state] / f"{clean}.json").resolve()
            if p.is_relative_to(d[state].resolve()) and p.is_file():
                p.unlink()
                return {"ok": True, "job_id": clean, "removed_from": state}
        raise FileNotFoundError(f"任务不存在: {job_id}")
