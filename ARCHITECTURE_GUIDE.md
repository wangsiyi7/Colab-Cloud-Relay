# 架构与工程学习指南（Colab-Cloud-Relay）

这份文档提炼了 `Colab-Cloud-Relay` 背后 **5 个核心工程模式**，方便你在阅读源码和把模块复用到其他系统时快速掌握设计思想。

---

## 1. 基于云盘 FUSE 的“无服务器”原子文件队列（Atomic File-Queue）

**问题**：Google Colab 运行在一个临时虚拟机里，没有公网固定 IP，本地电脑也不应该暴露端口给公网。那本地电脑如何可靠地向云端下发任务并接收进度？

**解法（见 [queue_engine.py](colab_cloud_relay/queue_engine.py)）**：
利用双方都能同时看到的 **Google Drive 共享文件系统**，用目录状态替代消息中间件：
- `queue/<job_id>.json`：本地投递的待办工单；
- `running/<job_id>.json`：Colab 领取后立刻原子移动并写入 `started_at`；
- `done/<job_id>.json` / `failed/<job_id>.json`：执行完并写回回执；
- `worker_heartbeat.json`：Colab 每轮运行更新心跳时间戳与磁盘剩余空间。

**关键技巧——为什么写文件要“先写 `.tmp` 再 `replace()`”？**
```python
tmp = d["queue"] / f".{job_id}.tmp"
tmp.write_text(json.dumps(ticket, ...), encoding="utf-8")
tmp.replace(target)
```
Google Drive 桌面客户端会监听目录变化。如果直接写正式 `.json`，客户端可能在写到一半时就把残缺 JSON 传上云导致解析失败；先写隐藏临时文件 `.tmp`，写完再同一文件系统原子 `replace()`，云端永远只能看到完整 JSON。

---

## 2. 异步“生产者-消费者”并发下载 + SVIP10 分段 Range

**问题**：如果目录里有上万个文件，先扫描完整个目录树再下载会导致界面长期卡在 `scanning`，且大文件单连接下载跑不满 SVIP10 带宽。

**解法（见 [notebook_builder.py](colab_cloud_relay/notebook_builder.py) 中的 `ColabRelayWorker.execute_ticket`）**：
1. **边扫边下（Streaming Producer-Consumer）**：主协程调用百度 `iter_files` 广度优先遍历，每扫到一个文件就塞入有界队列 `asyncio.Queue(maxsize=64)`；后台启动 `workers` 个协程并发从队列领文件开始写 Drive。
2. **大文件 HTTP `206 Partial Content` 并发分段**：对于 `>= 32 MiB` 的文件，先发 `Range: bytes=0-0` 探测服务器是否支持分块，再切分成 `8 MiB` 分片，用 `asyncio.Semaphore(parallel_ranges)` 控制并发协程数，通过文件定位 `fh.seek(s_b)` 并发写入同一个 `.bdpart` 临时文件，最后重命名为正式文件。

---

## 3. 零明文密钥隔离（Zero-Secret Credential Isolation）

**原则**：凡是会被同步到 Google Drive 或提交到 GitHub 的文件（`.ipynb`、`queue/*.json`、`done/*.json`），**绝对禁止出现任何 Cookie 或 Token**。

**实现（见 [config.py](colab_cloud_relay/config.py) & `notebook_builder.py`）**：
- `.ipynb` 在运行时从 Google 官方隔离保险箱 `google.colab.userdata.get('BAIDU_COOKIE')` 读取凭据，且凭据只驻留在内存变量里。
- 本地配置 [fastapi_plugin.py](colab_cloud_relay/fastapi_plugin.py) 中的 `/copy-cookie` 与 `/open-colab` 严格校验 `request.client.host in ("127.0.0.1", "::1", "localhost", "testclient")`，拒绝任何局域网/公网跨主机请求读取本地 Cookie。

---

## 4. 传输后“严格校验 → 强制刷盘 → 立即切断释放算力”

**问题**：Google Colab 算力按运行时活跃时间消耗计算单元；同时 Google Drive 在 Colab 里是 **FUSE 用户态虚拟文件系统**，Python `close()` 返回并不代表 Google Drive 后台已经把缓存刷到云端服务器——如果立刻断连，最后一秒写入的文件或回执可能丢失。

**解法（见 `_compute_final_status`、`verify_transferred_files`、`flush_drive_fs`、`disconnect_colab_runtime`）**：
1. **落盘完整性校验**：核对每个目标文件确实存在、不带 `.bdpart`/`.part` 临时后缀、且 `dest.stat().st_size == expected_size`；
2. **明确写回 `_sync_manifest.json` 终态**：用共享函数 `_compute_final_status(stats)` 计算并记录最终状态（`completed` / `paused_daily_limit` / `partial_failed` / `failed`）；
3. **FUSE 刷盘等待**：调用 `os.sync()` + `await asyncio.sleep(...)` 等候写入缓冲区刷新；
4. **自动释放计算单元**：更新 `worker_heartbeat.json` 为 `disconnected_after_verify`，随即调用 `google.colab.runtime.unassign()` 自动断开连接，从根源上杜绝算力浪费。

---

## 5. 路由工厂模式与跨生态打包（FastAPI Plugin + FastMCP + CLI）

**思想**：不要把配置写死在全局变量或某个单一项目中，而是暴露“组装工厂”：
- **FastAPI 路由工厂**：`create_colab_relay_router(prefix="/api/colab-relay")` 返回一个独立 `APIRouter`，任何 FastAPI 宿主应用（例如 `NotebookLM-Fleet`）只需 `app.include_router(...)` 即可立刻拥有完整能力；
- **独立 MCP 服务**：[mcp_server.py](colab_cloud_relay/mcp_server.py) 把同样的底层 `ColabQueueManager` 与 `auto_configure` 封装为 7 个 AI Agent 工具；
- **独立 `pyproject.toml` 入口**：通过 `console_scripts` 同时提供 CLI（`colab-relay`）与 MCP（`colab-relay-mcp`）。
