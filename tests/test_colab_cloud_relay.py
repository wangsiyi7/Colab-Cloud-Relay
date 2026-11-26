"""Tests for standalone Colab-Cloud-Relay package, FastAPI plugin, queue, and MCP."""

from __future__ import annotations

import ast
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from colab_cloud_relay import (
    ColabQueueManager,
    auto_configure,
    build_master_worker_notebook,
    create_colab_relay_router,
)


@pytest.fixture()
def isolated_relay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    drive_root = tmp_path / "DriveRoot"
    drive_root.mkdir(parents=True, exist_ok=True)
    nlm_hub = drive_root / "NLM_Hub"
    nlm_hub.mkdir(parents=True, exist_ok=True)

    cfg_path = tmp_path / "relay_cfg" / "config.json"
    auth_file = tmp_path / "baidupan_auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "mode": "cookie",
                "cookies": {
                    "BDUSS": "relay_test_secret_bduss_123456",
                    "STOKEN": "relay_test_secret_stoken_789",
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("COLAB_RELAY_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("BAIDUPAN_AUTH_FILE", str(auth_file))
    return {"drive": drive_root, "cfg": cfg_path, "auth": auth_file}


def test_master_notebook_ast_and_zero_secrets() -> None:
    nb = build_master_worker_notebook()
    assert nb["nbformat"] == 4
    assert nb["nbformat_minor"] == 5
    code_cells = [c for c in nb["cells"] if c["cell_type"] == "code"]
    assert len(code_cells) == 4
    for idx, cell in enumerate(code_cells):
        src = "".join(cell["source"])
        if idx == 0:
            # Skip shell magic line `!pip install -q httpx` before ast.parse
            src = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("!"))
        ast.parse(src)
    dumped = json.dumps(nb, ensure_ascii=False)
    assert "userdata.get('BAIDU_COOKIE')" in dumped
    assert "relay_test_secret" not in dumped
    assert "once_and_disconnect" in dumped
    assert "runtime.unassign()" in dumped
    assert "verify_transferred_files" in dumped
    assert "os.sync()" in dumped


def test_auto_configure_queue_lifecycle_and_fastapi_plugin(
    isolated_relay: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("colab_cloud_relay.config.copy_text_to_clipboard", lambda _t: True)

    app = FastAPI()
    app.include_router(create_colab_relay_router(prefix="/api/colab-relay"))

    with TestClient(app) as client:
        # 1. Auto-configure initializes ColabCloudRelay + mirrors into NLM_Hub/04_Colab_Jobs
        r_cfg = client.post(
            "/api/colab-relay/auto-configure",
            json={"drive_root": str(isolated_relay["drive"])},
        )
        assert r_cfg.status_code == 200
        cfg_res = r_cfg.json()
        assert cfg_res["ok"] is True
        assert cfg_res["baidu_ready"] is True
        assert cfg_res["baidu_has_stoken"] is True
        assert Path(cfg_res["master_notebook_path"]).is_file()
        assert Path(cfg_res["mirrored_nlm_hub_path"]).is_file()
        assert "relay_test_secret_bduss_123456" not in json.dumps(cfg_res, ensure_ascii=False)

        # 2. Enqueue jobs (baidu_to_gdrive + url_to_gdrive)
        r_e1 = client.post(
            "/api/colab-relay/jobs/enqueue",
            json={
                "kind": "baidu_to_gdrive",
                "source_paths": ["/Course/TB_Data"],
                "target_dir": "我的同步",
                "daily_limit_gb": 700,
            },
        )
        assert r_e1.status_code == 200
        t1 = r_e1.json()["ticket"]
        assert t1["job_id"].startswith("CR-")
        assert Path(t1["ticket_path"]).is_file()

        r_e2 = client.post(
            "/api/colab-relay/jobs/enqueue",
            json={
                "kind": "url_to_gdrive",
                "urls": ["https://example.com/paper.pdf"],
                "target_dir": "论文离线",
            },
        )
        assert r_e2.status_code == 200
        t2 = r_e2.json()["ticket"]

        # 3. Simulated Colab cloud worker heartbeat + completion
        qm = ColabQueueManager(isolated_relay["drive"])
        (qm.root / "worker_heartbeat.json").write_text(
            json.dumps({"status": "idle", "ts_epoch": time.time(), "drive_free_gb": 1800.0, "processed_jobs": 1}),
            encoding="utf-8",
        )
        q_file = qm.dirs()["queue"] / f"{t1['job_id']}.json"
        done_file = qm.dirs()["done"] / f"{t1['job_id']}.json"
        done_payload = {**t1, "status": "completed", "completed": 5, "skipped": 0, "failed": 0, "bytes": 102400}
        done_file.write_text(json.dumps(done_payload, ensure_ascii=False), encoding="utf-8")
        q_file.unlink()

        r_st = client.get("/api/colab-relay/status")
        assert r_st.status_code == 200
        st = r_st.json()
        assert st["worker_heartbeat"]["online"] is True
        assert st["queue_counts"]["done"] == 1
        assert st["queue_counts"]["queue"] == 1

        # 4. Retry & Delete & Master download & Loopback cookie/open-colab
        r_ret = client.post(f"/api/colab-relay/jobs/{t1['job_id']}/retry")
        assert r_ret.status_code == 200
        assert r_ret.json()["ticket"]["job_id"] != t1["job_id"]

        r_del = client.delete(f"/api/colab-relay/jobs/{t2['job_id']}")
        assert r_del.status_code == 200

        r_dl = client.get("/api/colab-relay/master-notebook/download")
        assert r_dl.status_code == 200
        assert r_dl.json()["nbformat"] == 4

        r_ck = client.post("/api/colab-relay/copy-cookie", json={"copy_to_clipboard": True})
        assert r_ck.status_code == 200
        assert "relay_test_secret_bduss_123456" in r_ck.json()["cookie_header"]

        r_open = client.post("/api/colab-relay/open-colab", json={"copy_cookie_to_clipboard": True})
        assert r_open.status_code == 200
        assert r_open.json()["cookie_copied_to_clipboard"] is True
        assert "github/wangsiyi7/Colab-Cloud-Relay" in r_open.json()["url"]


def test_mcp_server_clean_stdio_and_tools() -> None:
    code = (
        "import asyncio, json; "
        "from colab_cloud_relay.mcp_server import mcp; "
        "tools = asyncio.run(mcp.list_tools()); "
        "print(json.dumps([t.name for t in tools]))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln]
    assert len(lines) == 1
    names = json.loads(lines[0])
    assert len(names) == 7
    assert "relay_auto_config" in names
    assert "relay_enqueue_baidu_to_gdrive" in names
    assert "relay_open_colab" in names
