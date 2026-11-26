"""Configuration, Google Drive mount discovery, and local cookie discovery for Colab-Cloud-Relay."""

from __future__ import annotations

import json
import os
import shutil
import string
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

GITHUB_REPO_DEFAULT = "wangsiyi7/Colab-Cloud-Relay"
MASTER_NOTEBOOK_FILENAME = "Colab_Cloud_Relay.ipynb"
DEFAULT_RELAY_SUBDIR = "ColabCloudRelay"


def get_colab_github_url(repo: str = GITHUB_REPO_DEFAULT) -> str:
    clean = (repo or GITHUB_REPO_DEFAULT).strip().strip("/")
    return f"https://colab.research.google.com/github/{clean}/blob/main/notebooks/{MASTER_NOTEBOOK_FILENAME}"


def get_config_path() -> Path:
    override = (os.environ.get("COLAB_RELAY_CONFIG_PATH") or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".colab_cloud_relay" / "config.json"


@dataclass
class RelayConfig:
    drive_root: str = ""
    relay_subdir: str = DEFAULT_RELAY_SUBDIR
    baidu_auth_file: str = ""
    github_repo: str = GITHUB_REPO_DEFAULT
    colab_drive_url: str = ""
    default_gdrive_subdir: str = "百度网盘同步"
    default_baidu_dir: str = "/NotebookLM_同步"
    default_workers: int = 4
    default_parallel_ranges: int = 4
    default_daily_limit_gb: int = 700
    secret_configured: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_config() -> RelayConfig:
    cfg_path = get_config_path()
    if cfg_path.is_file():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                known = {k: v for k, v in raw.items() if k in RelayConfig.__dataclass_fields__}
                return RelayConfig(**known)
        except Exception:  # noqa: BLE001
            pass
    return RelayConfig()


def save_config(cfg: RelayConfig) -> Path:
    cfg_path = get_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg_path


def detect_google_drive_roots() -> list[str]:
    """Detect mounted Google Drive root folders across Windows, macOS, and Linux."""
    found: list[str] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        key = str(p).rstrip("/\\").lower()
        if key not in seen and p.is_dir():
            seen.add(key)
            found.append(str(p))

    if sys.platform == "win32":
        for letter in string.ascii_uppercase:
            for sub in ("我的云端硬盘", "My Drive"):
                candidate = Path(f"{letter}:/{sub}")
                if candidate.is_dir():
                    _add(candidate)
    else:
        cloud_storage = Path.home() / "Library" / "CloudStorage"
        if cloud_storage.is_dir():
            for child in sorted(cloud_storage.glob("GoogleDrive-*")):
                for sub in ("My Drive", "我的云端硬盘"):
                    if (child / sub).is_dir():
                        _add(child / sub)
        for fallback in (Path.home() / "Google Drive" / "My Drive", Path.home() / "MyDrive"):
            if fallback.is_dir():
                _add(fallback)
    return found


def discover_local_baidu_auth_files(extra_roots: list[str] | None = None) -> list[str]:
    """Read-only discovery of existing local BaiduPan auth files (never reads/exposes cookies here)."""
    candidates: list[Path] = []
    env_file = (os.environ.get("BAIDUPAN_AUTH_FILE") or "").strip()
    if env_file:
        candidates.append(Path(env_file))

    for root in extra_roots or []:
        if root and str(root).strip():
            candidates.append(Path(root) / "data" / "baidupan_auth.json")
            candidates.append(Path(root) / "baidupan_auth.json")

    cwd = Path.cwd()
    repo_parent = Path(__file__).resolve().parents[2]
    for base in (cwd, cwd.parent, repo_parent):
        candidates.append(base / "data" / "baidupan_auth.json")
        candidates.append(base / "NotebookLM-Fleet" / "data" / "baidupan_auth.json")
        candidates.append(base / "BaiduPan-MCP" / "data" / "baidupan_auth.json")

    candidates.append(Path.home() / ".baidupan_mcp" / "auth.json")

    out: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        try:
            res = str(c.resolve())
        except OSError:
            res = str(c)
        if res not in seen and c.is_file():
            seen.add(res)
            out.append(res)
    return out


def _parse_cookie_dict(raw: str) -> dict[str, str]:
    text = (raw or "").strip()
    if "=" not in text:
        return {"BDUSS": text} if text else {}
    jar: dict[str, str] = {}
    for seg in text.split(";"):
        if "=" not in seg:
            continue
        k, v = seg.strip().split("=", 1)
        if k.strip():
            jar[k.strip()] = v.strip().strip('"').strip("'")
    return jar


def read_baidu_cookie_header(auth_file: str | Path | None = None) -> dict[str, Any]:
    """Safely inspect local baidupan_auth.json and return cookie header strictly for loopback copy."""
    candidates = [str(auth_file)] if auth_file else discover_local_baidu_auth_files()
    for p_str in candidates:
        p = Path(p_str)
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        mode = str(data.get("mode") or "")
        cookies: dict[str, str] = {}
        if isinstance(data.get("cookies"), dict):
            cookies = {str(k): str(v) for k, v in data["cookies"].items() if v}
        elif isinstance(data.get("cookie"), str):
            cookies = _parse_cookie_dict(data["cookie"])
        if cookies.get("BDUSS"):
            header = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
            return {
                "authenticated": True,
                "mode": mode or "cookie",
                "has_stoken": bool(cookies.get("STOKEN")),
                "auth_file": str(p),
                "cookie_header": header,
            }
    return {
        "authenticated": False,
        "mode": None,
        "has_stoken": False,
        "auth_file": candidates[0] if candidates else "",
        "cookie_header": "",
    }


def copy_text_to_clipboard(text: str) -> bool:
    """Copy `text` cleanly to the OS clipboard via stdin (never prints secrets to stdout)."""
    if not text:
        return False
    if sys.platform == "win32":
        for cmd in (
            ["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"],
            ["clip.exe"],
        ):
            try:
                proc = subprocess.run(cmd, input=text, text=True, capture_output=True, timeout=6)
                if proc.returncode == 0:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(["pbcopy"], input=text, text=True, capture_output=True, timeout=5)
            return proc.returncode == 0
        except Exception:  # noqa: BLE001
            return False
    for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        if shutil.which(cmd[0]):
            try:
                proc = subprocess.run(cmd, input=text, text=True, capture_output=True, timeout=5)
                if proc.returncode == 0:
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def auto_configure(
    *,
    drive_root: str | None = None,
    baidu_auth_file: str | None = None,
    github_repo: str | None = None,
    relay_subdir: str | None = None,
) -> dict[str, Any]:
    """Auto-detect Google Drive, initialize relay folders, deploy master .ipynb, and link Baidu auth."""
    from .notebook_builder import write_master_notebook
    from .queue_engine import ColabQueueManager

    cfg = load_config()
    if github_repo and github_repo.strip():
        cfg.github_repo = github_repo.strip().strip("/")
    if relay_subdir and relay_subdir.strip():
        cfg.relay_subdir = relay_subdir.strip().strip("/\\")

    detected_drives = detect_google_drive_roots()
    if drive_root and drive_root.strip():
        cfg.drive_root = drive_root.strip()
    elif not cfg.drive_root or not Path(cfg.drive_root).is_dir():
        cfg.drive_root = detected_drives[0] if detected_drives else str(Path.home() / "ColabCloudRelay_Local")

    discovered_auths = discover_local_baidu_auth_files()
    if baidu_auth_file and baidu_auth_file.strip():
        cfg.baidu_auth_file = baidu_auth_file.strip()
    elif (not cfg.baidu_auth_file or not Path(cfg.baidu_auth_file).is_file()) and discovered_auths:
        cfg.baidu_auth_file = discovered_auths[0]

    qm = ColabQueueManager(cfg.drive_root, relay_subdir=cfg.relay_subdir)
    dirs = qm.ensure_dirs()
    master_nb_path = dirs["notebooks"] / MASTER_NOTEBOOK_FILENAME
    write_master_notebook(master_nb_path)

    mirrored_nlm_path = ""
    nlm_hub_jobs = Path(cfg.drive_root) / "NLM_Hub" / "04_Colab_Jobs"
    if nlm_hub_jobs.parent.is_dir():
        nlm_hub_jobs.mkdir(parents=True, exist_ok=True)
        nlm_target = nlm_hub_jobs / MASTER_NOTEBOOK_FILENAME
        write_master_notebook(nlm_target)
        mirrored_nlm_path = str(nlm_target)

    saved_cfg = save_config(cfg)
    auth_probe = read_baidu_cookie_header(cfg.baidu_auth_file or None)

    return {
        "ok": True,
        "config_path": str(saved_cfg),
        "drive_root": cfg.drive_root,
        "drive_mounted": Path(cfg.drive_root).is_dir(),
        "detected_drives": detected_drives,
        "relay_dir": str(dirs["root"]),
        "queue_dir": str(dirs["queue"]),
        "master_notebook_path": str(master_nb_path),
        "mirrored_nlm_hub_path": mirrored_nlm_path,
        "baidu_auth_file": cfg.baidu_auth_file,
        "baidu_ready": bool(auth_probe["authenticated"]),
        "baidu_has_stoken": bool(auth_probe["has_stoken"]),
        "colab_github_url": get_colab_github_url(cfg.github_repo),
    }
