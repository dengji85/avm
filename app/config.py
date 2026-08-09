# -*- coding: utf-8 -*-
"""配置读写。

所有运行期数据（数据库、封面、配置）都放在项目根目录的 data/ 下，
便于整体备份或迁移。
"""
from __future__ import annotations

import copy
import json
import sys
import threading
from pathlib import Path
from typing import Any, Dict

if getattr(sys, "frozen", False):
    # PyInstaller 打包后运行：数据目录放在 exe 同级（便于备份/迁移），
    # 前端静态资源在解包目录（_MEIPASS）里。
    BASE_DIR = Path(sys.executable).resolve().parent
    _MEIPASS = Path(getattr(sys, "_MEIPASS", str(BASE_DIR)))
    WEB_DIR = _MEIPASS / "web_dist"
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
    WEB_DIR = BASE_DIR / "web_dist"
DATA_DIR = BASE_DIR / "data"
COVER_DIR = DATA_DIR / "covers"
AVATAR_DIR = DATA_DIR / "avatars"
FANART_DIR = DATA_DIR / "fanarts"
DB_PATH = DATA_DIR / "library.db"
CONFIG_PATH = DATA_DIR / "config.json"


def _resolve_dir(cfg_value: str | None, default: Path) -> Path:
    """解析目录配置：相对路径基于 DATA_DIR，绝对路径直接使用（便于迁移/独立磁盘）。"""
    if not cfg_value:
        return default
    p = Path(str(cfg_value))
    if not p.is_absolute():
        p = DATA_DIR / p
    return p


def avatar_dir(cfg: Dict[str, Any] | None = None) -> Path:
    cfg = cfg or load_config()
    return _resolve_dir(cfg.get("media", {}).get("avatar_dir"), AVATAR_DIR)


def fanart_dir(cfg: Dict[str, Any] | None = None) -> Path:
    cfg = cfg or load_config()
    return _resolve_dir(cfg.get("media", {}).get("fanart_dir"), FANART_DIR)


_LOCK = threading.RLock()
_CACHE: Dict[str, Any] | None = None

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

DEFAULT_CONFIG: Dict[str, Any] = {
    "library": {
        # 需要扫描的根目录，例如 "D:/Media/Movies"
        "paths": [],
        "video_extensions": [
            ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".ts", ".m2ts", ".m4v",
            ".flv", ".rmvb", ".rm", ".mpg", ".mpeg", ".webm", ".vob", ".iso", ".strm",
        ],
        # 小于该体积的文件视为预览片/样片，直接跳过
        "min_size_mb": 100,
        # 扫描完成后自动清理失效记录（磁盘已删的文件 / 空影片 / 孤儿元数据）
        "auto_cleanup": True,
        "ignore_keywords": ["sample", "trailer", "预告", "花絮", "特典"],
        "ignore_dirs": [
            "@eaDir", "#recycle", "$RECYCLE.BIN", "System Volume Information",
            ".git", ".svn", "extrafanart", "extrathumbs", "BDMV", "CERTIFICATE",
        ],
        "follow_symlinks": False,
    },
    "cover": {
        # 扫描时自动嗅探同目录下的本地封面图
        "auto_local": True,
        # 允许从元数据源返回的 URL 下载封面
        "download": True,
        "timeout": 20,
        # 没有封面时用番号生成占位图
        "placeholder": True,
    },
    "scraper": {
        "enabled": True,
        # 抓取顺序，先命中的先用；可选 javbus / javdb / avwiki / local_nfo / http_json / http_html
        # avwiki 置前：优先用其素人化名→真名映射补齐素人片元数据，封面回退到 javbus/javdb
        "order": ["avwiki", "javbus", "javdb", "local_nfo"],
        "timeout": 20,
        # 并发抓取线程数；网络 IO 密集型，适度提高可显著加速（建议 2-8）
        "workers": 4,
        # 每条请求之间的间隔（毫秒），避免把数据源打挂
        "delay_ms": 800,
        "proxy": "",
        "user_agent": DEFAULT_UA,
        "overwrite": False,  # 是否覆盖已有的人工编辑结果
        "javbus": {
            "enabled": True,
            "name": "JavBus",
            "base_url": "https://www.javbus.com",
            # 从浏览器复制的 Cloudflare clearance Cookie，格式：cf_clearance=xxxx; 其他=yyy
            # 仅在被 Cloudflare 拦截时填写，留空则尝试直连
            "cookie": "",
        },
        "javdb": {
            "enabled": True,
            "name": "JavDB",
            "base_url": "https://javdb.com",
            "cookie": "",
        },
        "avwiki": {
            "enabled": True,
            "name": "AV-Wiki (素人)",
            "base_url": "https://av-wiki.net",
        },
        "http_json": {
            "enabled": False,
            "name": "自定义 JSON 接口",
            # {code} 会被替换成番号，例如 http://127.0.0.1:8080/api/movie/{code}
            "url": "",
            "headers": {},
            # JSON 中数据对象的位置，支持点号与数组下标，如 "data.0"，留空表示根节点
            "root": "",
            # 左边是本工具字段，右边是数据源 JSON 里的取值路径
            "fields": {
                "title": "title",
                "original_title": "original_title",
                "release_date": "release_date",
                "runtime": "runtime",
                "studio": "studio",
                "publisher": "publisher",
                "series": "series",
                "director": "director",
                "plot": "plot",
                "rating": "rating",
                "cover": "cover",
                "actresses": "actresses",
                "genres": "genres",
            },
        },
        "http_html": {
            "enabled": False,
            "name": "自定义网页规则",
            # 详情页地址模板，{code} 会被替换成番号
            "detail_url": "",
            # 若需要先搜索再进详情页，填搜索地址模板 + 结果链接选择器
            "search_url": "",
            "result_link_selector": "",
            "headers": {},
            # 每个字段: {"css": "选择器", "attr": "text|src|href|...", "multi": false, "regex": ""}
            "selectors": {
                "title": {"css": "", "attr": "text"},
                "cover": {"css": "", "attr": "src"},
                "release_date": {"css": "", "attr": "text", "regex": "(\\d{4}-\\d{2}-\\d{2})"},
                "runtime": {"css": "", "attr": "text", "regex": "(\\d+)"},
                "studio": {"css": "", "attr": "text"},
                "publisher": {"css": "", "attr": "text"},
                "series": {"css": "", "attr": "text"},
                "director": {"css": "", "attr": "text"},
                "plot": {"css": "", "attr": "text"},
                "rating": {"css": "", "attr": "text", "regex": "([\\d.]+)"},
                "actresses": {"css": "", "attr": "text", "multi": True},
                "genres": {"css": "", "attr": "text", "multi": True},
            },
        },
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8770,
        "open_browser": True,
    },
    "ui": {
        "page_size": 60,
        "default_sort": "added_desc",
    },
    "media": {
        # 媒体资源本地落盘目录（相对于 data/，或填绝对路径以支持独立磁盘/迁移）
        "avatar_dir": "avatars",
        "fanart_dir": "fanarts",
        # 是否从刮削源下载并落盘；关闭则仅保留远程 URL，不占本地空间
        "avatar_download": False,
        "fanart_download": True,
    },
    "ai": {
        # AI 增强（可选）。兼容 OpenAI 协议的任意端点：
        #  - 云端：https://api.openai.com/v1
        #  - 国产/中转：硅基流动、DeepSeek、通义等（同协议）
        #  - 本地 Ollama：http://127.0.0.1:11434/v1 （需先 ollama pull qwen2.5，免 key）
        "enabled": False,
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "temperature": 0.4,
    },
}


def ensure_dirs() -> None:
    # 注意：不能调用 load_config()（否则会再次触发 ensure_dirs 造成递归）。
    # 仅创建固定默认目录；自定义 media 目录在运行时由 avatar_dir()/fanart_dir() 按需创建。
    for d in (DATA_DIR, COVER_DIR, AVATAR_DIR, FANART_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _deep_merge(base: Any, patch: Any) -> Any:
    """把 patch 合并进 base（不修改入参），dict 递归合并，其余直接覆盖。"""
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for k, v in patch.items():
            out[k] = _deep_merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(patch)


def load_config(refresh: bool = False) -> Dict[str, Any]:
    global _CACHE
    with _LOCK:
        if _CACHE is not None and not refresh:
            return copy.deepcopy(_CACHE)
        ensure_dirs()
        data: Dict[str, Any] = {}
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                backup = CONFIG_PATH.with_suffix(".bak.json")
                try:
                    CONFIG_PATH.replace(backup)
                except Exception:
                    pass
                data = {}
        merged = _deep_merge(DEFAULT_CONFIG, data)
        _CACHE = merged
        if not CONFIG_PATH.exists():
            _write(merged)
        return copy.deepcopy(merged)


def _write(cfg: Dict[str, Any]) -> None:
    ensure_dirs()
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def save_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    global _CACHE
    with _LOCK:
        merged = _deep_merge(DEFAULT_CONFIG, cfg)
        _write(merged)
        _CACHE = merged
        return copy.deepcopy(merged)


def update_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    """局部更新配置并落盘，返回完整配置。"""
    with _LOCK:
        current = load_config()
        return save_config(_deep_merge(current, patch))


def roots_to_scan(cfg: Dict[str, Any] | None = None) -> list:
    """返回需要扫描的根目录列表，兼容旧字段 roots / 新字段 paths。"""
    cfg = cfg or load_config()
    paths = cfg.get("library", {}).get("paths") or cfg.get("library", {}).get("roots") or []
    return [str(p) for p in paths if p]


def video_exts(cfg: Dict[str, Any] | None = None) -> list:
    """返回视频扩展名列表，兼容 video_extensions / video_exts。"""
    cfg = cfg or load_config()
    exts = cfg.get("library", {}).get("video_extensions") or cfg.get("library", {}).get("video_exts") or []
    return list(exts)
