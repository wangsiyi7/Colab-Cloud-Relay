"""Builds the Master Permanent Queue-Driven Google Colab Worker Notebook (nbformat 4.5).

SECURITY INVARIANT:
Never embeds plaintext cookies, tokens, or personal credentials anywhere inside `.ipynb` files.
Credentials are loaded strictly at runtime via Google Colab Secrets (`BAIDU_COOKIE`) or `getpass`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import DEFAULT_RELAY_SUBDIR, GITHUB_REPO_DEFAULT, get_colab_github_url


def _md_cell(lines: list[str]) -> dict[str, Any]:
    return {
        "cell_type": "markdown",
        "metadata": {},
        "source": [ln if ln.endswith("\n") else ln + "\n" for ln in lines],
    }


def _code_cell(lines: list[str]) -> dict[str, Any]:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": [ln if ln.endswith("\n") else ln + "\n" for ln in lines],
    }


_WORKER_ENGINE_SRC = '''# Colab-Cloud-Relay · Permanent Multi-Engine Worker (Baidu SVIP10 / URL Offline / Google Drive)
import asyncio
import base64
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import shutil
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import httpx

WEB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
PCS_UA = "netdisk;P2SP;3.0.0.8;netdisk;11.12.3;PC;PC-Windows"
SLICE_SIZE = 4 * 1024 * 1024
PARALLEL_THRESHOLD = 32 * 1024 * 1024
RANGE_CHUNK = 8 * 1024 * 1024


def norm_pan(p: str) -> str:
    p = (p or "/").strip().replace("\\\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    res = posixpath.normpath(p)
    return res if res.startswith("/") else "/" + res


def safe_seg(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\\\|?*\\x00-\\x1f]+', "_", (name or "").strip()).strip(" .")
    return cleaned or "_"


def is_excluded(name: str, full_path: str, patterns: list[str]) -> bool:
    for pat in patterns or []:
        if fnmatch.fnmatch(name, pat) or (full_path and fnmatch.fnmatch(full_path, pat)):
            return True
    return False


def baidu_sign2(sign3: str, sign1: str) -> str:
    if not sign3 or not sign1:
        return ""
    a = list(range(256))
    p = [ord(sign3[i % len(sign3)]) for i in range(256)]
    u = 0
    for i in range(256):
        u = (u + a[i] + p[i]) % 256
        a[i], a[u] = a[u], a[i]
    i = u = 0
    out = bytearray()
    for ch in sign1:
        i = (i + 1) % 256
        u = (u + a[i]) % 256
        a[i], a[u] = a[u], a[i]
        out.append((ord(ch) ^ a[(a[i] + a[u]) % 256]) & 0xFF)
    return base64.b64encode(bytes(out)).decode("ascii")


def parse_cookies(raw: str) -> dict[str, str]:
    text = (raw or "").strip()
    if "=" not in text:
        return {"BDUSS": text} if text else {}
    jar = {}
    for part in text.split(";"):
        if "=" not in part:
            continue
        k, v = part.strip().split("=", 1)
        if k.strip():
            jar[k.strip()] = v.strip().strip('"').strip("'")
    return jar


class ColabBaiduClient:
    def __init__(self, cookie_header: str) -> None:
        self.cookie_str = "; ".join(f"{k}={v}" for k, v in parse_cookies(cookie_header).items() if v)
        if "BDUSS=" not in self.cookie_str:
            raise RuntimeError("BAIDU_COOKIE 缺少 BDUSS，无法执行百度网盘搬运；请先在 Colab Secrets 配好 BAIDU_COOKIE。")
        self.bdstoken = ""
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=600.0, write=300.0, pool=30.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
        )

    def web_hdrs(self) -> dict[str, str]:
        return {"User-Agent": WEB_UA, "Referer": "https://pan.baidu.com/disk/main", "Cookie": self.cookie_str}

    def pcs_hdrs(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"User-Agent": PCS_UA, "Referer": "https://pan.baidu.com/disk/main", "Cookie": self.cookie_str}
        if extra:
            h.update(extra)
        return h

    async def req_json(self, method: str, url: str, *, params=None, data=None) -> dict:
        for attempt in range(4):
            try:
                r = await self.client.request(method, url, params=params, data=data, headers=self.web_hdrs())
                r.raise_for_status()
                d = r.json()
                errno = d.get("errno")
                if errno not in (None, 0):
                    raise RuntimeError(f"Baidu errno={errno}: {d}")
                return d
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError):
                if attempt == 3:
                    raise
                await asyncio.sleep(0.8 * (2 ** attempt))
        raise RuntimeError("Unreachable")

    async def init_user(self) -> dict:
        tpl = await self.req_json(
            "GET",
            "https://pan.baidu.com/api/gettemplatevariable",
            params={"fields": json.dumps(["bdstoken", "sign1", "sign3", "timestamp", "username"]), "channel": "chunlei", "web": 1, "app_id": 250528},
        )
        res = tpl.get("result") or {}
        self.bdstoken = str(res.get("bdstoken") or "")
        return res

    async def list_dir(self, path: str) -> list[dict]:
        norm = norm_pan(path)
        items = []
        start = 0
        while True:
            d = await self.req_json(
                "GET",
                "https://pan.baidu.com/api/list",
                params={"dir": norm, "order": "name", "start": start, "limit": 500, "web": 1, "channel": "chunlei", "app_id": 250528},
            )
            batch = d.get("list") or []
            items.extend(batch)
            if len(batch) < 500:
                break
            start += len(batch)
        return items

    async def iter_files(self, src_path: str):
        norm = norm_pan(src_path)
        if norm != "/":
            parent_items = await self.list_dir(posixpath.dirname(norm))
            hit = next((x for x in parent_items if norm_pan(x.get("path", "")) == norm), None)
            if not hit:
                raise RuntimeError(f"百度路径不存在: {norm}")
            if not int(hit.get("isdir", 0)):
                yield hit
                return
        q = deque([norm])
        while q:
            cur = q.popleft()
            for it in await self.list_dir(cur):
                if int(it.get("isdir", 0)):
                    q.append(norm_pan(it["path"]))
                else:
                    yield it

    async def resolve_urls(self, rpath: str, fs_id: int) -> list[str]:
        urls = []
        try:
            res = await self.init_user()
            sign = baidu_sign2(str(res.get("sign3") or ""), str(res.get("sign1") or ""))
            ts = str(res.get("timestamp") or "")
            if sign and ts:
                dl = await self.req_json(
                    "GET",
                    "https://pan.baidu.com/api/download",
                    params={"sign": sign, "timestamp": ts, "fidlist": json.dumps([int(fs_id)]), "type": "dlink", "channel": "chunlei", "web": 1, "app_id": 250528},
                )
                for e in dl.get("dlink") or []:
                    if e.get("dlink"):
                        urls.append(e["dlink"])
        except Exception:
            pass
        try:
            r = await self.client.get(
                "https://d.pcs.baidu.com/rest/2.0/pcs/file",
                params={"method": "locatedownload", "app_id": 250528, "ver": "4.0", "path": rpath},
                headers=self.pcs_hdrs(),
            )
            if r.status_code == 200:
                for e in r.json().get("urls") or []:
                    if e.get("url"):
                        urls.append(e["url"])
        except Exception:
            pass
        urls.append(f"https://d.pcs.baidu.com/rest/2.0/pcs/file?method=download&app_id=250528&path={quote(rpath, safe='/')}")
        return list(dict.fromkeys(urls))

    async def download_to_drive(self, item: dict, dest: Path, parallel_ranges: int) -> int:
        rpath = norm_pan(item["path"])
        fs_id = int(item.get("fs_id") or 0)
        size = int(item.get("size") or 0)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".bdpart")
        urls = await self.resolve_urls(rpath, fs_id)
        last_err = None
        for u in urls:
            try:
                if size >= PARALLEL_THRESHOLD and not part.exists():
                    probe = await self.client.get(u, headers=self.pcs_hdrs({"Range": "bytes=0-0"}))
                    if probe.status_code == 206:
                        with part.open("wb") as fh:
                            fh.seek(size - 1)
                            fh.write(b"\\x00")
                        ranges = [(off, min(size - 1, off + RANGE_CHUNK - 1)) for off in range(0, size, RANGE_CHUNK)]
                        sem = asyncio.Semaphore(parallel_ranges)
                        wlock = asyncio.Lock()

                        async def _one(s_b: int, e_b: int) -> None:
                            async with sem:
                                resp = await self.client.get(u, headers=self.pcs_hdrs({"Range": f"bytes={s_b}-{e_b}"}))
                                if resp.status_code not in (200, 206):
                                    raise RuntimeError(f"HTTP {resp.status_code}")
                                async with wlock:
                                    with part.open("r+b") as fh:
                                        fh.seek(s_b)
                                        fh.write(resp.content)

                        await asyncio.gather(*(_one(s_b, e_b) for s_b, e_b in ranges))
                        if part.stat().st_size == size:
                            part.replace(dest)
                            return size

                existing = part.stat().st_size if part.exists() else 0
                hdrs = self.pcs_hdrs({"Range": f"bytes={existing}-"} if existing > 0 else None)
                async with self.client.stream("GET", u, headers=hdrs) as resp:
                    if resp.status_code == 416 and existing > 0:
                        part.replace(dest)
                        return dest.stat().st_size
                    if resp.status_code not in (200, 206):
                        raise RuntimeError(f"HTTP {resp.status_code}")
                    mode = "ab" if resp.status_code == 206 else "wb"
                    with part.open(mode) as fh:
                        async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                            if chunk:
                                fh.write(chunk)
                if size == 0 or part.stat().st_size == size:
                    part.replace(dest)
                    return dest.stat().st_size
            except Exception as exc:
                last_err = exc
        raise RuntimeError(f"全部下载通道失败 ({rpath}): {last_err}")

    async def mkdir_pan(self, remote_dir: str) -> None:
        norm = norm_pan(remote_dir)
        if norm == "/":
            return
        if not self.bdstoken:
            await self.init_user()
        cur = ""
        for p in [seg for seg in norm.strip("/").split("/") if seg]:
            cur = f"{cur}/{p}"
            try:
                await self.req_json(
                    "POST",
                    "https://pan.baidu.com/api/create",
                    params={"a": "commit", "channel": "chunlei", "web": 1, "app_id": 250528, "bdstoken": self.bdstoken},
                    data={"path": cur, "isdir": 1, "block_list": "[]"},
                )
            except RuntimeError as exc:
                if "errno=-8" not in str(exc):
                    raise

    async def upload_from_drive(self, src: Path, remote_path: str) -> int:
        norm = norm_pan(remote_path)
        size = src.stat().st_size
        if size <= PARALLEL_THRESHOLD:
            with src.open("rb") as fh:
                r = await self.client.post(
                    "https://d.pcs.baidu.com/rest/2.0/pcs/file",
                    params={"method": "upload", "app_id": 250528, "path": norm, "ondup": "overwrite"},
                    headers=self.pcs_hdrs(),
                    files={"file": (src.name, fh, "application/octet-stream")},
                )
            r.raise_for_status()
            return size

        if not self.bdstoken:
            await self.init_user()
        blocks = []
        with src.open("rb") as fh:
            while True:
                buf = fh.read(SLICE_SIZE)
                if not buf:
                    break
                blocks.append(hashlib.md5(buf).hexdigest())
        bjson = json.dumps(blocks or [hashlib.md5(b"").hexdigest()])
        pre = await self.req_json(
            "POST",
            "https://pan.baidu.com/api/precreate",
            params={"channel": "chunlei", "web": 1, "app_id": 250528, "bdstoken": self.bdstoken},
            data={"path": norm, "size": str(size), "isdir": "0", "autoinit": "1", "rtype": "3", "block_list": bjson},
        )
        if int(pre.get("return_type") or 0) == 2:
            return size
        uid = pre["uploadid"]
        for i in range(len(blocks)):
            with src.open("rb") as fh:
                fh.seek(i * SLICE_SIZE)
                chunk = fh.read(SLICE_SIZE)
            r = await self.client.post(
                "https://d.pcs.baidu.com/rest/2.0/pcs/superfile2",
                params={"method": "upload", "type": "tmpfile", "app_id": 250528, "path": norm, "uploadid": uid, "partseq": i},
                headers=self.pcs_hdrs(),
                files={"file": (f"p{i}", chunk, "application/octet-stream")},
            )
            r.raise_for_status()
        await self.req_json(
            "POST",
            "https://pan.baidu.com/api/create",
            params={"channel": "chunlei", "web": 1, "app_id": 250528, "bdstoken": self.bdstoken},
            data={"path": norm, "size": str(size), "isdir": "0", "uploadid": uid, "block_list": bjson, "rtype": "3"},
        )
        return size


class ColabRelayWorker:
    def __init__(self, drive_root: Path, cookie_header: str = "", default_daily_limit_gb: int = 700, default_workers: int = 4, default_parallel_ranges: int = 4) -> None:
        self.drive_root = Path(drive_root)
        self.relay_root = self.drive_root / "ColabCloudRelay"
        self.cookie_header = cookie_header
        self.default_daily_limit_gb = default_daily_limit_gb
        self.default_workers = default_workers
        self.default_parallel_ranges = default_parallel_ranges
        self.processed_jobs = 0

    def ensure_dirs(self) -> dict[str, Path]:
        dirs = {k: self.relay_root / k for k in ("queue", "running", "done", "failed", "logs", "notebooks")}
        for p in dirs.values():
            p.mkdir(parents=True, exist_ok=True)
        return dirs

    async def flush_drive_fs(self, wait_sec: int = 3) -> None:
        """Force Linux/FUSE buffer flush so Google Drive syncs receipts and files before disconnect."""
        try:
            if hasattr(os, "sync"):
                os.sync()
        except Exception:
            pass
        if wait_sec > 0:
            await asyncio.sleep(wait_sec)

    async def disconnect_colab_runtime(self, reason: str, grace_sec: int = 5) -> None:
        """Flush Google Drive FUSE, mark heartbeat disconnected, and unassign Colab runtime."""
        print(f"\\n🔌 [省算力自动切断] {reason}，等待 {grace_sec}s 确保 Google Drive 落盘同步...")
        await self.flush_drive_fs(wait_sec=max(1, int(grace_sec)))
        self.write_heartbeat("disconnected_after_verify", extra={"auto_disconnected": True, "disconnect_reason": reason})
        await self.flush_drive_fs(wait_sec=2)
        try:
            from google.colab import runtime
            print("🛑 正在调用 google.colab.runtime.unassign() 释放当前计算单元...")
            runtime.unassign()
        except Exception as exc:
            print(f"ℹ️ 非 Colab 容器或无法调用 runtime.unassign(): {exc}")

    def verify_transferred_files(self, verified_targets: list[tuple[Path, int]]) -> dict:
        errors = []
        verified_count = 0
        for dest, expected_size in verified_targets:
            if not dest.exists():
                errors.append(f"校验失败: 文件未落盘 {dest}")
                continue
            if dest.name.endswith((".bdpart", ".part")):
                errors.append(f"校验失败: 仍为临时后缀 {dest.name}")
                continue
            actual = dest.stat().st_size
            if expected_size > 0 and actual != expected_size:
                errors.append(f"校验失败: 大小不匹配 {dest.name} (期望 {expected_size}, 实际 {actual})")
                continue
            verified_count += 1
        return {
            "verification_passed": len(errors) == 0,
            "verified_count": verified_count,
            "verification_errors": errors[:50],
        }

    def write_heartbeat(self, status: str, current_job_id: str = "", extra: dict | None = None) -> None:
        usage = shutil.disk_usage(self.drive_root)
        mem_gb = None
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    mem_gb = round(int(line.split()[1]) / (1024 * 1024), 1)
                    break
        except Exception:
            pass
        hb = {
            "status": status,
            "ts_epoch": time.time(),
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "drive_free_gb": round(usage.free / (1024 ** 3), 2),
            "ram_gb": mem_gb,
            "current_job_id": current_job_id,
            "processed_jobs": self.processed_jobs,
            "baidu_cookie_ready": bool(self.cookie_header and "BDUSS=" in self.cookie_header),
        }
        if extra:
            hb.update(extra)
        tmp = self.relay_root / ".worker_heartbeat.tmp"
        tmp.write_text(json.dumps(hb, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.relay_root / "worker_heartbeat.json")

    async def execute_ticket(self, ticket: dict) -> dict:
        kind = ticket.get("kind") or ticket.get("direction") or "baidu_to_gdrive"
        t0 = time.time()
        stats = {
            "completed": 0,
            "skipped": 0,
            "failed": 0,
            "bytes": 0,
            "paused_for_daily_limit": False,
            "verified_count": 0,
            "verification_passed": True,
            "verification_errors": [],
            "errors": [],
        }
        verified_targets: list[tuple[Path, int]] = []
        excludes = ticket.get("exclude_patterns") or []
        preserve = bool(ticket.get("preserve_tree", True))
        skip_exist = bool(ticket.get("skip_existing", True))
        workers_n = max(1, min(int(ticket.get("workers") or self.default_workers), 8))
        ranges_n = max(1, min(int(ticket.get("parallel_ranges") or self.default_parallel_ranges), 8))
        limit_gb = int(ticket.get("daily_limit_gb") if ticket.get("daily_limit_gb") is not None else self.default_daily_limit_gb)
        limit_bytes = limit_gb * (1024 ** 3) if limit_gb > 0 else 0

        if kind in ("baidu_to_gdrive", "gdrive_to_baidu"):
            cli = ColabBaiduClient(self.cookie_header)
            try:
                if kind == "baidu_to_gdrive":
                    sub = str(ticket.get("target_dir") or "百度网盘同步").strip().strip("/\\\\") or "百度网盘同步"
                    dest_root = self.drive_root.joinpath(*(safe_seg(s) for s in re.split(r"[/\\\\]+", sub) if s))
                    dest_root.mkdir(parents=True, exist_ok=True)
                    q: asyncio.Queue = asyncio.Queue(maxsize=64)

                    async def _worker() -> None:
                        while True:
                            job = await q.get()
                            try:
                                if job is None:
                                    return
                                item, dest = job
                                if limit_bytes > 0 and stats["bytes"] >= limit_bytes:
                                    stats["paused_for_daily_limit"] = True
                                    continue
                                sz = int(item.get("size") or 0)
                                if skip_exist and dest.exists() and (sz == 0 or dest.stat().st_size == sz):
                                    stats["skipped"] += 1
                                    verified_targets.append((dest, sz))
                                    continue
                                nbytes = await cli.download_to_drive(item, dest, ranges_n)
                                stats["completed"] += 1
                                stats["bytes"] += nbytes
                                verified_targets.append((dest, sz or nbytes))
                                print(f"[OK] {dest.relative_to(self.drive_root)} ({nbytes/1048576:.2f} MB)")
                            except Exception as exc:
                                stats["failed"] += 1
                                stats["errors"].append(f"{job[0].get('path') if job else ''}: {exc}")
                                print(f"[ERR] {exc}")
                            finally:
                                q.task_done()

                    wtasks = [asyncio.create_task(_worker()) for _ in range(workers_n)]
                    for src in ticket.get("source_paths") or []:
                        if stats["paused_for_daily_limit"] or (limit_bytes > 0 and stats["bytes"] >= limit_bytes):
                            stats["paused_for_daily_limit"] = True
                            break
                        nsrc = norm_pan(src)
                        base_name = safe_seg(posixpath.basename(nsrc.rstrip("/")) or "root")
                        async for item in cli.iter_files(nsrc):
                            if stats["paused_for_daily_limit"] or (limit_bytes > 0 and stats["bytes"] >= limit_bytes):
                                stats["paused_for_daily_limit"] = True
                                break
                            ipath = norm_pan(item["path"])
                            fname = item.get("server_filename") or posixpath.basename(ipath)
                            if is_excluded(fname, ipath, excludes):
                                stats["skipped"] += 1
                                continue
                            if not preserve or ipath == nsrc:
                                dest = dest_root / safe_seg(fname)
                            else:
                                rel = posixpath.relpath(ipath, nsrc)
                                parts = [safe_seg(s) for s in rel.split("/") if s and s not in (".", "..")]
                                dest = dest_root.joinpath(base_name, *parts) if nsrc != "/" else dest_root.joinpath(*parts)
                            await q.put((item, dest))
                    for _ in wtasks:
                        await q.put(None)
                    await asyncio.gather(*wtasks)
                    await self.flush_drive_fs(wait_sec=2)
                    v_res = self.verify_transferred_files(verified_targets)
                    stats.update(v_res)
                    if not v_res["verification_passed"]:
                        stats["failed"] += len(v_res["verification_errors"])
                        stats["errors"].extend(v_res["verification_errors"])
                    (dest_root / "_sync_manifest.json").write_text(
                        json.dumps({**ticket, **stats, "elapsed_sec": round(time.time() - t0, 1)}, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                else:
                    target_pan = norm_pan(ticket.get("target_dir") or "/NotebookLM_同步")
                    await cli.mkdir_pan(target_pan)
                    for raw_local in ticket.get("source_paths") or []:
                        rel = str(raw_local).strip().lstrip("/\\\\")
                        src_p = Path(raw_local) if str(raw_local).startswith("/content/") else (self.drive_root / rel)
                        if not src_p.exists():
                            stats["failed"] += 1
                            stats["errors"].append(f"Google Drive 路径不存在: {src_p}")
                            continue
                        files = [src_p] if src_p.is_file() else [p for p in src_p.rglob("*") if p.is_file()]
                        for fp in files:
                            if is_excluded(fp.name, fp.as_posix(), excludes):
                                stats["skipped"] += 1
                                continue
                            rel_part = fp.name if not preserve or src_p.is_file() else f"{src_p.name}/{fp.relative_to(src_p).as_posix()}"
                            r_dest = norm_pan(posixpath.join(target_pan, rel_part))
                            await cli.mkdir_pan(posixpath.dirname(r_dest))
                            nbytes = await cli.upload_from_drive(fp, r_dest)
                            stats["completed"] += 1
                            stats["bytes"] += nbytes
                            print(f"[UP OK] {r_dest} ({nbytes/1048576:.2f} MB)")
            finally:
                await cli.client.aclose()
        elif kind == "url_to_gdrive":
            sub = str(ticket.get("target_dir") or "Colab离线下载").strip().strip("/\\\\") or "Colab离线下载"
            dest_root = self.drive_root.joinpath(*(safe_seg(s) for s in re.split(r"[/\\\\]+", sub) if s))
            dest_root.mkdir(parents=True, exist_ok=True)
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=600.0), follow_redirects=True) as hcli:
                for u in ticket.get("urls") or []:
                    try:
                        parsed = urlparse(u)
                        raw_name = safe_seg(unquote(posixpath.basename(parsed.path)) or f"download_{int(time.time())}")
                        dest = dest_root / raw_name
                        if skip_exist and dest.exists() and dest.stat().st_size > 0:
                            stats["skipped"] += 1
                            verified_targets.append((dest, dest.stat().st_size))
                            continue
                        part = dest.with_name(dest.name + ".part")
                        written = 0
                        async with hcli.stream("GET", u) as resp:
                            resp.raise_for_status()
                            with part.open("wb") as fh:
                                async for ch in resp.aiter_bytes(1024 * 1024):
                                    if ch:
                                        fh.write(ch)
                                        written += len(ch)
                        part.replace(dest)
                        stats["completed"] += 1
                        stats["bytes"] += written
                        verified_targets.append((dest, written))
                        print(f"[URL OK] {dest.name} ({written/1048576:.2f} MB)")
                    except Exception as exc:
                        stats["failed"] += 1
                        stats["errors"].append(f"{u}: {exc}")
            await self.flush_drive_fs(wait_sec=2)
            stats.update(self.verify_transferred_files(verified_targets))
        elif kind == "gdrive_organize":
            sub = str(ticket.get("target_dir") or "").strip().strip("/\\\\")
            scan_dir = self.drive_root.joinpath(*(safe_seg(s) for s in re.split(r"[/\\\\]+", sub) if s)) if sub else self.drive_root
            file_count = 0
            total_sz = 0
            top_files = []
            if scan_dir.exists():
                for fp in scan_dir.rglob("*"):
                    if fp.is_file():
                        try:
                            sz = fp.stat().st_size
                            file_count += 1
                            total_sz += sz
                            top_files.append((sz, str(fp.relative_to(self.drive_root))))
                        except OSError:
                            pass
            top_files.sort(reverse=True)
            stats["completed"] = file_count
            stats["bytes"] = total_sz
            stats["organize_report"] = {"root": str(scan_dir), "files": file_count, "total_bytes": total_sz, "largest_10": [{"path": p, "bytes": s} for s, p in top_files[:10]]}
        else:
            raise ValueError(f"Unknown ticket kind: {kind}")

        stats["elapsed_sec"] = round(time.time() - t0, 1)
        stats["errors"] = stats["errors"][:200]
        return stats

    def scan_queues(self) -> list[Path]:
        dirs = self.ensure_dirs()
        queues = [dirs["queue"]]
        nlm_q = self.drive_root / "NLM_Hub" / "04_Colab_Jobs" / "queue"
        if nlm_q.is_dir():
            queues.append(nlm_q)
        found = []
        for qd in queues:
            found.extend(sorted((p for p in qd.glob("*.json") if not p.name.startswith(".")), key=lambda p: p.stat().st_mtime))
        return found

    async def process_once(self, will_auto_disconnect: bool = False) -> int:
        dirs = self.ensure_dirs()
        tickets = self.scan_queues()
        handled = 0
        self.last_batch_auto_disconnect = False
        for tpath in tickets:
            try:
                ticket = json.loads(tpath.read_text(encoding="utf-8"))
            except Exception:
                continue
            job_id = str(ticket.get("job_id") or tpath.stem)
            running_path = dirs["running"] / f"{job_id}.json"
            ticket["status"] = "running"
            ticket["started_at"] = datetime.now(timezone.utc).isoformat()
            running_path.write_text(json.dumps(ticket, ensure_ascii=False, indent=2), encoding="utf-8")
            tpath.unlink(missing_ok=True)
            self.write_heartbeat("running", current_job_id=job_id)
            print(f"\\n>>> [START] {job_id} ({ticket.get('kind')}) - {ticket.get('title', '')}")
            try:
                stats = await self.execute_ticket(ticket)
                if stats.get("paused_for_daily_limit"):
                    final_status = "paused_daily_limit"
                elif int(stats.get("failed") or 0) > 0:
                    final_status = "partial_failed" if int(stats.get("completed") or 0) > 0 else "failed"
                else:
                    final_status = "completed"
                if bool(ticket.get("auto_disconnect")):
                    self.last_batch_auto_disconnect = True
                result = {
                    **ticket,
                    **stats,
                    "status": final_status,
                    "auto_disconnected": bool(will_auto_disconnect or ticket.get("auto_disconnect")),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
                dest_dir = dirs["failed"] if final_status == "failed" else dirs["done"]
                (dest_dir / f"{job_id}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                rel_rec = (ticket.get("receipt_drive_rel_path") or "").strip().lstrip("/\\\\")
                if rel_rec:
                    rp = self.drive_root / rel_rec
                    rp.parent.mkdir(parents=True, exist_ok=True)
                    rp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                running_path.unlink(missing_ok=True)
                self.processed_jobs += 1
                handled += 1
                print(
                    f"<<< [DONE] {job_id}: {final_status} | completed={stats['completed']} "
                    f"skipped={stats['skipped']} verified={stats.get('verified_count', 0)} "
                    f"verification_passed={stats.get('verification_passed')} failed={stats['failed']} bytes={stats['bytes']}"
                )
            except Exception as exc:
                err_res = {**ticket, "status": "failed", "errors": [str(exc)], "finished_at": datetime.now(timezone.utc).isoformat()}
                (dirs["failed"] / f"{job_id}.json").write_text(json.dumps(err_res, ensure_ascii=False, indent=2), encoding="utf-8")
                running_path.unlink(missing_ok=True)
                print(f"<<< [FAIL] {job_id}: {exc}")
        return handled

    async def run_forever(
        self,
        *,
        mode: str = "once_and_disconnect",
        poll_interval_sec: int = 15,
        max_idle_minutes: int = 5,
        auto_disconnect: bool = True,
        disconnect_grace_seconds: int = 5,
    ) -> None:
        self.ensure_dirs()
        last_active = time.time()
        disconnected_cleanly = False
        print(f"🚀 Colab-Cloud-Relay 已启动 | 模式={mode} | 完结自动切断={auto_disconnect} | 监听目录={self.relay_root / 'queue'}")
        try:
            while True:
                self.write_heartbeat("idle")
                will_dc = auto_disconnect and (mode == "once_and_disconnect")
                cnt = await self.process_once(will_auto_disconnect=will_dc)
                if cnt > 0:
                    last_active = time.time()
                if mode == "once_and_disconnect":
                    await self.flush_drive_fs(wait_sec=2)
                    if auto_disconnect:
                        disconnected_cleanly = True
                        await self.disconnect_colab_runtime(
                            f"已处理 {cnt} 个工单并完成文件完整性校验",
                            grace_sec=disconnect_grace_seconds,
                        )
                    else:
                        print(f"✅ once_and_disconnect 单轮完成（已关闭自动切断，共处理 {cnt} 个工单）。")
                    break
                if mode == "once":
                    await self.flush_drive_fs(wait_sec=2)
                    print(f"✅ 单次巡检已完成（处理 {cnt} 个工单）。")
                    break
                if cnt > 0 and auto_disconnect and getattr(self, "last_batch_auto_disconnect", False):
                    disconnected_cleanly = True
                    await self.disconnect_colab_runtime("收到工单 auto_disconnect=true，校验完立即释放算力", grace_sec=disconnect_grace_seconds)
                    break
                self.write_heartbeat("idle")
                if max_idle_minutes > 0 and (time.time() - last_active) >= max_idle_minutes * 60:
                    if auto_disconnect:
                        disconnected_cleanly = True
                        await self.disconnect_colab_runtime(f"空闲超过 {max_idle_minutes} 分钟，自动释放计算单元", grace_sec=disconnect_grace_seconds)
                    else:
                        print(f"⏸️ 空闲超过 {max_idle_minutes} 分钟，自动休眠退出。")
                    break
                await asyncio.sleep(max(3, int(poll_interval_sec)))
        finally:
            if not disconnected_cleanly:
                self.write_heartbeat("stopped")
'''


def build_master_worker_notebook(*, github_repo: str = GITHUB_REPO_DEFAULT, relay_subdir: str = DEFAULT_RELAY_SUBDIR) -> dict[str, Any]:
    """Build the standalone permanent queue-driven Colab Worker `.ipynb` (nbformat 4.5, zero secrets)."""
    gh_url = get_colab_github_url(github_repo)
    cells = [
        _md_cell(
            [
                "# ⚡ Colab-Cloud-Relay · Google Colab Ultra 零 VPN 云端直传（省算力自动切断版）\n",
                "\n",
                f"> **独立开源插件仓库**：`{github_repo}` · [在 Colab 打开最新版]({gh_url})  \n",
                "> **适用场景**：**Google AI Ultra（1000 积分） + 百度网盘 SVIP10** 或离线 URL 直存 Google 云盘  \n",
                "> **省算力保护机制**：默认 `once_and_disconnect` 模式——传输完成 → **字节大小与落盘完整性校验** → `os.sync()` 刷盘 → **立即调用 `runtime.unassign()` 自动切断释放计算单元**！\n",
                "\n",
                "### 10 秒首次部署（只需配置一次 `BAIDU_COOKIE`，后续永久复用）\n",
                "1. 在本地电脑运行 **`一键自动配置环境`** 或 **`打开 Colab + 复制 Cookie`**，你的百度 Cookie 已自动复制到剪贴板；\n",
                "2. 点击 Colab 最左侧竖栏 **🔑 Secrets（密钥）** → **+ 添加新密钥**：名称填 `BAIDU_COOKIE`，粘贴并打开访问开关；\n",
                "3. 右上角 **更改运行时类型** 选 **`CPU` + `高内存（High-RAM）`**（几乎不消耗 Ultra 1000 积分！），按 **`Ctrl + F9`（全部运行）**；传输并校验完会自动切断！",
            ]
        ),
        _code_cell(
            [
                "# 第 1 步：安装依赖并挂载 Google 云盘（Google 机房内网直写极快）",
                "!pip install -q httpx",
                "from google.colab import drive",
                "from pathlib import Path",
                "import shutil",
                "",
                "drive.mount('/content/drive')",
                "DRIVE_ROOT = Path('/content/drive/MyDrive')",
                f"RELAY_ROOT = DRIVE_ROOT / {relay_subdir!r}",
                "for sub in ('queue', 'running', 'done', 'failed', 'logs', 'notebooks'):",
                "    (RELAY_ROOT / sub).mkdir(parents=True, exist_ok=True)",
                "usage = shutil.disk_usage(DRIVE_ROOT)",
                "print(f'✅ Google Drive 已挂载 | 队列根目录: {RELAY_ROOT} | 可用空间: {usage.free / (1024**3):.1f} GB')",
            ]
        ),
        _code_cell(
            [
                "# 第 2 步：安全读取百度网盘 Cookie（优先读取 Colab Secrets: BAIDU_COOKIE，绝不写入 .ipynb）",
                "import getpass",
                "BAIDU_COOKIE = ''",
                "try:",
                "    from google.colab import userdata",
                "    BAIDU_COOKIE = (userdata.get('BAIDU_COOKIE') or '').strip()",
                "except Exception:",
                "    pass",
                "if not BAIDU_COOKIE:",
                "    print('ℹ️ 未检测到 Secret BAIDU_COOKIE；若需搬运百度网盘请粘贴，若仅做 URL 离线下载可直接回车跳过：')",
                "    BAIDU_COOKIE = getpass.getpass('BAIDU_COOKIE (输入不可见): ').strip()",
                "if 'BDUSS=' in BAIDU_COOKIE:",
                "    print('✅ 已加载百度 SVIP10 凭据（仅驻留内存）')",
                "else:",
                "    print('⚠️ 未提供 BAIDU_COOKIE，当前仅可执行 url_to_gdrive / gdrive_organize 任务')",
            ]
        ),
        _code_cell(_WORKER_ENGINE_SRC.splitlines()),
        _code_cell(
            [
                "# 第 3 步：执行工单并自动校验落盘 + 立即切断释放算力（防止空转浪费计算单元）",
                'RELAY_MODE = "once_and_disconnect" #@param ["once_and_disconnect", "once", "daemon"]',
                'AUTO_DISCONNECT_AFTER_VERIFY = True #@param {type:"boolean"}',
                'IDLE_DISCONNECT_MINUTES = 5 #@param {type:"integer"}',
                'DISCONNECT_GRACE_SECONDS = 5 #@param {type:"integer"}',
                'POLL_INTERVAL_SEC = 15 #@param {type:"integer"}',
                'DEFAULT_DAILY_LIMIT_GB = 700 #@param {type:"integer"}',
                'DEFAULT_WORKERS = 4 #@param {type:"integer"}',
                'DEFAULT_PARALLEL_RANGES = 4 #@param {type:"integer"}',
                "",
                "worker = ColabRelayWorker(",
                "    DRIVE_ROOT,",
                "    cookie_header=BAIDU_COOKIE,",
                "    default_daily_limit_gb=DEFAULT_DAILY_LIMIT_GB,",
                "    default_workers=DEFAULT_WORKERS,",
                "    default_parallel_ranges=DEFAULT_PARALLEL_RANGES,",
                ")",
                "await worker.run_forever(",
                "    mode=RELAY_MODE,",
                "    poll_interval_sec=POLL_INTERVAL_SEC,",
                "    max_idle_minutes=IDLE_DISCONNECT_MINUTES,",
                "    auto_disconnect=AUTO_DISCONNECT_AFTER_VERIFY,",
                "    disconnect_grace_seconds=DISCONNECT_GRACE_SECONDS,",
                ")",
            ]
        ),
    ]

    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.11"},
            "colab": {"name": "Colab_Cloud_Relay.ipynb", "provenance": []},
        },
        "cells": cells,
    }


def write_master_notebook(target_path: str | Path, *, github_repo: str = GITHUB_REPO_DEFAULT) -> Path:
    dest = Path(target_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    nb = build_master_worker_notebook(github_repo=github_repo)
    dest.write_text(json.dumps(nb, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest
