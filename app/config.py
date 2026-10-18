# -*- coding: utf-8 -*-
"""配置读写。

所有运行期数据（数据库、封面、配置）都放在项目根目录的 data/ 下，
便于整体备份或迁移。
"""
from __future__ import annotations

import copy
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List

if getattr(sys, "frozen", False):
    # PyInstaller 打包后运行：数据目录放在 exe 同级（便于备份/迁移），
    # 前端静态资源在解包目录（_MEIPASS）里。
    BASE_DIR = Path(sys.executable).resolve().parent
    _MEIPASS = Path(getattr(sys, "_MEIPASS", str(BASE_DIR)))
    WEB_DIR = _MEIPASS / "web_dist"
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
    WEB_DIR = BASE_DIR / "web_dist"
# 数据目录：默认项目根 / exe 同级的 data/；容器 / NAS 部署用 AVM_DATA_DIR 指向挂载卷。
_env_data_dir = os.environ.get("AVM_DATA_DIR", "").strip()
DATA_DIR = Path(_env_data_dir).expanduser() if _env_data_dir else BASE_DIR / "data"
COVER_DIR = DATA_DIR / "covers"
AVATAR_DIR = DATA_DIR / "avatars"
FANART_DIR = DATA_DIR / "fanarts"
# 浏览器播放兼容层的重封装产物（-c:v copy 快速转封装为 MP4），可安全删除
REMUX_DIR = DATA_DIR / "remux"
DB_PATH = DATA_DIR / "library.db"
CONFIG_PATH = DATA_DIR / "config.json"
# 完整访问令牌的落盘文件（权限 0600）：启动日志只打印掩码，容器 / NAS 用户从这里取
TOKEN_FILE = DATA_DIR / "access_token.txt"
# 是否运行在容器内。容器经端口映射 / 同机反代时无法区分「真本机」，
# 因此「本机免令牌」在容器里默认关闭（详见 docs/安全与隐私设计.md）。
IN_CONTAINER = os.environ.get("AVM_CONTAINER", "").strip() == "1"


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

# 预置的常用在线站点。详情页「打开在线页面」直接用它，
# 用户不必先去设置页添加并保存一次才有得点；可在设置页自由增删改。
# lower=true 表示跳转前把 {code} 转为小写（JavBus / 123av 等路径用小写番号）。
# 与前端 SettingsView 的 PRESET_SITES 保持一致。
DEFAULT_ONLINE_SITES: List[Dict[str, Any]] = [
    {"id": "preset_javbus", "name": "JavBus", "url": "https://javbus.com/{code}", "lower": True, "enabled": True},
    {"id": "preset_javwiki", "name": "AV-Wiki", "url": "https://av-wiki.net/{code}/", "lower": False, "enabled": True},
    {"id": "preset_javdb", "name": "JavDB", "url": "https://javdb.com/search?q={code}", "lower": True, "enabled": True},
    {"id": "preset_javlibrary", "name": "JavLibrary",
     "url": "https://www.javlibrary.com/cn/vl_searchbyid.php?keyword={code}", "lower": False, "enabled": True},
    {"id": "preset_missav", "name": "123av", "url": "https://123av.com/cn/v/{code}", "lower": True, "enabled": True},
    {"id": "preset_jable", "name": "Jable", "url": "https://jable.tv/videos/{code}/", "lower": True, "enabled": True},
]

DEFAULT_CONFIG: Dict[str, Any] = {
    # 预览图密度：smart=按时长自适应 / low=稀疏省空间 / high=精细（影响抽帧张数）
    "previews_quality": "smart",
    # 常用在线站点：详情页一键打开该影片在各站的页面
    "online_sites": copy.deepcopy(DEFAULT_ONLINE_SITES),
    "library": {
        # 需要扫描的根目录，例如 "D:/Media/Movies"
        "paths": [],
        "video_extensions": [
            ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".ts", ".m2ts", ".m4v",
            ".flv", ".rmvb", ".rm", ".mpg", ".mpeg", ".webm", ".vob", ".iso", ".strm",
        ],
        # 小于该体积的文件视为预览片/样片，直接跳过
        "min_size_mb": 100,
        # 扫描时探测内嵌字幕（判断是否真的自带中文字幕，与元数据标注区分）。
        # 每部片多花 0.2–2 秒；已有库升级后第一次扫描会补测，之后按 size/mtime 增量跳过。
        "probe_subtitles": True,
        # 扫描完成后自动清理失效记录（磁盘已删的文件 / 空影片 / 孤儿元数据）
        "auto_cleanup": True,
        # 「文件在磁盘上消失」后的宽限天数：先标记缺失并保留记录，超过这个天数才在
        # 清理时真正删除。避免外置盘/网络盘临时掉线、下载器临时移走文件导致误删数据。
        # 0 = 不保留（扫描发现即清理，即旧行为）。
        "missing_grace_days": 30,
        # 删除影片文件时是否移入系统回收站（可恢复）。false = 永久删除，不可恢复。
        "delete_to_recycle_bin": True,
        # 扫描时忽略的临时/未完成文件后缀（下载器边下边写，避免把半成品入库）
        "ignore_suffixes": [".part", ".tmp", ".!qb", ".aria2", ".crdownload", ".downloading", ".partial"],
        # 自动扫描间隔（分钟）。0=关闭，后台会按间隔增量扫描媒体库。
        "auto_scan_interval": 0,
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
        # 刮削任务结束后，自动为仍无封面的影片从视频抽帧兜底（临时占位，刮削到正式海报会覆盖）
        "auto_extract": True,
        # 允许从元数据源返回的 URL 下载封面
        "download": True,
        "timeout": 20,
        # 没有封面时用番号生成占位图
        "placeholder": True,
    },
    "stream": {
        # 浏览器播放失败时，自动用 ffmpeg 重封装为 MP4 再试：
        # 视频流直接复制（-c:v copy，零重编码、几乎不耗 CPU），仅音轨转 AAC。
        # 仅当视频编码本身可被浏览器解码（H.264）时才重封装；HEVC 等直接走系统播放器。
        "auto_remux": True,
        # 重封装缓存目录（data/remux）大小上限 MB；超出后按最久未访问清理，0=不限制
        "remux_cache_mb": 4096,
        # 网络串流（.strm）：默认让浏览器直连目标地址（省服务器带宽）；
        # 源站有防盗链 / 不允许跨域时打开代理，视频字节全部经本服务中转。
        "strm_proxy": False,
        # 代理 / 拉播放列表时附加的 Referer 与 UA；留空 = 目标站自身 origin + 浏览器 UA
        "strm_referer": "",
        "strm_user_agent": "",
    },
    "scraper": {
        "enabled": True,
        # 抓取顺序，先命中的先用；可选 javbus / javdb / avwiki / local_nfo / http_json / http_html
        # local_nfo 置前：本地 nfo/json 是用户自己整理的权威数据，且完全离线、零延迟；
        # 影片没有 nfo 时它直接未命中，自动落到在线源，无额外成本。缺失字段由后续源补齐。
        # avwiki 固定沉底（见 providers.ALWAYS_LAST）：它只收录素人片且抓取开销大，
        # 作为补充源在最后补查女优真名，避免普通番号为它空转拖慢整体速度。
        "order": ["local_nfo", "javbus", "javdb", "avwiki"],
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
        # 对外访问地址（可选）：容器 / NAS 端口映射后，进程内只能看到容器网段，
        # 「设置 → 访问」里的地址会指向 172.17.x.x 这类手机连不上的地址。
        # 在这里填 http://192.168.1.10:8770（或用环境变量 AVM_PUBLIC_URL 覆盖），
        # 设置页就会显示该地址。
        "public_url": "",
        # 访问令牌：为空时自动生成。完整值同时写入 data/access_token.txt（0600），
        # 启动日志只打印掩码，避免令牌泄漏进 docker logs / 日志文件。
        # 设置页默认只显示掩码，查看完整值需再次输入令牌（见 /api/server/reveal-token）。
        "access_token": "",
        # 访问保护（唯一开关）：开启后**所有设备**（含本机 127.0.0.1）都必须用
        # 访问令牌换取会话；关闭则任何能连上的设备都可直接访问（设置页有红字警告）。
        # 不再区分「本机 / 远程」——只有令牌这一个凭据。
        # 旧键名 require_token_remote 会在加载时自动迁移到本键。
        "token_required": True,
        # 信任的反向代理 IP：只有直连来源位于该列表时才解析 X-Forwarded-For，
        # 否则忽略该头（防止伪造 IP 绕过登录限速与审计来源）。
        # NAS 上反代与容器同机时，保持默认即可拿到真实客户端 IP。
        "trusted_proxies": ["127.0.0.1", "::1"],
        # 会话（登录后签发）：空闲超时 / 绝对超时（天，0 = 不过期）
        "session_idle_days": 7,
        "session_abs_days": 30,
        # 会话是否落盘：默认开启 = 以 SHA-256 指纹写入 data/sessions.json（0600），
        # 容器重启 / 升级不掉登录；关闭则仅内存，重启即全部失效。
        "session_persist": True,
        # 登录失败限速：同一 IP 连续失败达到该次数后封禁指定分钟数（0 = 不限制）
        "login_max_fails": 5,
        "login_block_minutes": 15,
        # 是否允许匿名访问 /api/docs 与 /api/openapi.json（默认关闭）
        "expose_docs": False,
        # 跨源白名单：默认空 = 不启用 CORS（同源部署无需 CORS）
        "cors_origins": [],
        # 安全响应头（CSP / nosniff / Referrer-Policy / X-Frame-Options 等）
        "security_headers": True,
        # 出站 HTTPS 是否跳过证书校验（不推荐开启）
        "insecure_skip_verify": False,
        # 两步验证（TOTP）：启用后，用令牌换取会话时还需输入验证器 App 的 6 位码。
        # 注意：以下字段都是凭据或凭据材料，GET /api/config 不会下发（见 app/api.py）。
        "totp_enabled": False,
        "totp_secret": "",        # Base32 密钥
        "totp_last_step": 0,      # 最近一次通过的时间步，用于防重放
        "totp_recovery": [],      # 备用码哈希（一次性，用后即删）
        # 版本更新清单地址。默认指向 GitHub Releases（发版即打 tag 推到 git 即可自动生效）：
        # 后端会按 GitHub 结构解析 tag_name(版本)/html_url(下载页)/body(更新说明)/published_at(日期)。
        # 也可改为自建的版本清单 JSON（含 version/download_url/released/notes）。
        "update_feed": "https://api.github.com/repos/dengji85/avm/releases/latest",
        # 首次启动引导是否已完成（引导结束后置为 True，换浏览器也不会重复弹）
        "setup_done": False,
        # 首次向导第 3 步的选择："这台服务会被谁访问"
        #   private = 只有自己 / 家庭内网    public = 会被公网访问（域名 / 反代 / 端口映射）
        # 只用于完成页与后续安全提示（是否建议开两步验证），**不直接改认证策略**。
        "exposure": "private",
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
    "plugins": {
        # 女优资料插件开关：{插件id: true/false}。
        # 留空 = 全部启用（详见 plugins/base.enabled_plugins），这样新增插件也开箱可用。
        # 只有用户显式关掉的插件才会被写到这里（值为 false）。
        "enabled": {},
    },
    # 女优资料插件（维基百科）配置
    "wiki": {
        # 抓取时尝试的语言版优先级（日文维基 {{AV女優}} 模板最全，回退中文维基）
        "languages": ["ja", "zh"],
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
        # 单次请求读超时（秒）与失败重试次数；超时/重试只影响体验，不影响正确性
        "timeout": 60,
        "max_retries": 2,
        # 用户自定义的 system 提示词：{prompt_id: text}，缺省用内置默认值
        "prompts": {},
        },
    # 语音对白提取（可选组件，需自行安装 faster-whisper 与模型）
    "asr": {
        # 模型档位：small（快）/ medium（准但慢约 3 倍）
        "model": "small",
        # 语言：automatic 检测在短音频/环境音上很容易跑偏（实测把日语片判成挪威语），
        # 所以默认固定 ja；英文/中文片请自行改成 en / zh
        "language": "ja",
        # 依赖目录（pip --target 的安装位置）。留空则用系统 Python 环境。
        # 例：D:\program my\avm-asr\libs
        "libs_dir": "",
        # 模型根目录，下面放 faster-whisper-small / faster-whisper-medium。
        # 留空则用 data/models。例：D:\program my\avm-asr\models
        "model_dir": "",
    },
    # 隐私保护（桌面 / 大屏场景）：离开电脑时自动遮挡，避免"忘记关"的尴尬
    "privacy": {
        # 空闲自动锁定（分钟）：无操作达到该时长后遮挡全部内容并暂停播放；0 = 关闭。
        # 正在播放视频时不算空闲（看片中途不会被锁）。
        "idle_lock_minutes": 30,
        # 遮挡后用什么解锁（只有两种，都要主动输入，点击即解锁已移除）：
        #   pin   = 快速解锁码（4~6 位，PBKDF2 存哈希；高频场景用）
        #   token = 访问令牌（兜底方式始终可用，不受本设置影响）
        # 旧值 click 会在加载时自动迁移（有解锁码 → pin，否则 → token）。
        "lock_unlock": "token",
        # 快速解锁码的 PBKDF2-HMAC-SHA256 哈希（设置页写入，不存明文；空 = 未设置）
        "pin_hash": "",
        # 未设解锁码时，用访问令牌解锁的累计次数（用于前端提示「设个解锁码」）
        "token_unlocks": 0,
        # 紧急遮挡（"老板键"）快捷键：任意界面按下立即遮挡 + 静音，再按一次恢复
        "panic_key": "Ctrl+Shift+H",
        # 页面标题中性化：标题固定为品牌名，不显示影片信息
        "neutral_title": True,
        # 浏览器标题的自定义后缀（混淆 / 防窥）：最终标题为「片匣 - <后缀>」，
        # 留空则只显示品牌名。用于任务栏 / 标签页被旁人扫到时不起疑。
        "site_subtitle": "",
        # 切换到其他标签页 / 最小化时自动暂停播放，避免声音外泄
        "pause_when_hidden": True,
        # 启动即遮挡：打开页面先显示遮挡层，需手动解锁
        "lock_on_start": False,
    },
}


def ensure_dirs() -> None:
    # 注意：不能调用 load_config()（否则会再次触发 ensure_dirs 造成递归）。
    # 仅创建固定默认目录；自定义 media 目录在运行时由 avatar_dir()/fanart_dir() 按需创建。
    for d in (DATA_DIR, COVER_DIR, AVATAR_DIR, FANART_DIR, REMUX_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _migrate_online_sites(sites: Any) -> List[Dict[str, Any]]:
    """补齐早期保存的预设站点缺失的 lower（番号大小写）字段。

    最早一批预设没有 lower 字段，导致 JavBus / 123av / Jable 这类要求小写番号的
    站点被拼成大写番号、打开即 404。这里按 id 比对预设表只补缺失字段，
    不覆盖用户自定义的名称/链接/排序；非预设站点原样保留。
    """
    if not isinstance(sites, list):
        return []
    preset_lower = {s["id"]: bool(s.get("lower")) for s in DEFAULT_ONLINE_SITES}
    out: List[Dict[str, Any]] = []
    for s in sites:
        if not isinstance(s, dict):
            continue
        item = dict(s)
        pid = item.get("id")
        if pid in preset_lower and "lower" not in item:
            item["lower"] = preset_lower[pid]
        out.append(item)
    return out


def _deep_merge(base: Any, patch: Any) -> Any:
    """把 patch 合并进 base（不修改入参），dict 递归合并，其余直接覆盖。"""
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for k, v in patch.items():
            out[k] = _deep_merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(patch)


def _migrate_legacy(cfg: Dict[str, Any]) -> bool:
    """就地迁移旧配置键，返回是否发生过改动（改动才落盘）。

    - `server.local_bypass`：已删除的「本机免令牌」，直接丢弃
    - `server.require_token_remote` → `server.token_required`：改名，值保持
    - `privacy.lock_unlock = click`：该模式已移除，按是否设过解锁码迁移到 pin / token
    """
    changed = False
    server = cfg.get("server")
    if isinstance(server, dict):
        if "local_bypass" in server:
            server.pop("local_bypass", None)
            changed = True
        if "require_token_remote" in server:
            old = server.pop("require_token_remote")
            if "token_required" not in server:
                server["token_required"] = bool(old)
            changed = True
    privacy = cfg.get("privacy")
    if isinstance(privacy, dict):
        mode = str(privacy.get("lock_unlock") or "token")
        if mode not in ("pin", "token"):
            privacy["lock_unlock"] = "pin" if (privacy.get("pin_hash") or "").strip() else "token"
            changed = True
    return changed


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
        # 常用站点：未配置过时由 DEFAULT_CONFIG 提供预设；已保存的走补齐逻辑。
        # 用户若手动删空了站点列表（空数组），这里保持为空，不再填回预设。
        merged["online_sites"] = _migrate_online_sites(merged.get("online_sites"))
        # 旧键迁移（单一令牌 / 隐私锁两态）：只在确有改动时写回一次
        migrated = _migrate_legacy(merged)
        _CACHE = merged
        if migrated or not CONFIG_PATH.exists():
            _write(merged)
        return copy.deepcopy(merged)


def _write(cfg: Dict[str, Any]) -> None:
    ensure_dirs()
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)
    # config.json 内含访问令牌与 TOTP 密钥明文，权限须与访问令牌文件一致（0600）：
    # 否则同机低权限进程可直接读走密钥，绕过两步验证。Windows 上 chmod 语义有限，
    # 失败静默跳过（与 token / sessions / api_keys 文件同一处理方式）。
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


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


def current_access_token() -> str:
    """当前生效的访问令牌（环境变量优先，其次配置文件），不生成、不落盘。

    中间件每个请求都要比对令牌，不能用 ensure_access_token()（它会写文件）；
    同时必须尊重 AVM_ACCESS_TOKEN —— 否则容器用环境变量预设令牌时，
    `?token=` 链接与 X-Access-Token 头会全部失效（配置里是空串）。
    """
    env = os.environ.get("AVM_ACCESS_TOKEN", "").strip()
    if env:
        return env
    try:
        return str((load_config().get("server") or {}).get("access_token") or "").strip()
    except Exception:
        return ""


def session_cookie_name() -> str:
    """会话 Cookie 名，按实例区分。

    Cookie 以**主机名**为作用域、**不区分端口**：同一台机器上同时跑开发实例
    （8770）与容器（8771）时，两个实例若都用 `avm_session`，后登录的那个会覆盖
    前一个的会话 Cookie，而两者的会话存储互不相通 —— 表现就是「刷新 / 打开播放
    时随机又要求输入令牌」。这里用数据目录派生的短哈希做后缀，天然互不干扰。

    也可用环境变量 AVM_COOKIE_NAME 显式指定（多实例共用同一数据目录时用得上）。
    """
    env = os.environ.get("AVM_COOKIE_NAME", "").strip()
    if env:
        return env
    import hashlib
    try:
        seed = str(DATA_DIR.resolve())
    except Exception:
        seed = str(DATA_DIR)
    return "avm_session_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]


def public_base_url() -> str:
    """返回对外访问地址（不带尾斜杠），未配置时返回 ""。

    容器 / NAS 经端口映射时，进程内探测到的只是容器网段地址，手机连不上。
    因此对外地址只能由外部显式告知：优先环境变量 AVM_PUBLIC_URL，
    其次配置项 server.public_url；都为空则沿用自动探测。
    """
    val = os.environ.get("AVM_PUBLIC_URL", "").strip()
    if not val:
        try:
            val = str((load_config().get("server") or {}).get("public_url") or "").strip()
        except Exception:
            val = ""
    return val.rstrip("/")


def ensure_access_token() -> str:
    """启动时确保存在一个访问令牌：若配置为空则生成一个随机令牌并落盘。

    返回当前生效的令牌。所有设备（含本机）都用它换取会话（推荐），
    或直接携带令牌（兼容脚本与旧客户端）；本机不再免令牌。

    容器 / NAS 部署可用环境变量 AVM_ACCESS_TOKEN 预设，避免首次启动还要翻日志找令牌。
    完整令牌同时写入 data/access_token.txt（权限 0600），启动日志只打印掩码。
    """
    env_tok = os.environ.get("AVM_ACCESS_TOKEN", "").strip()
    tok = env_tok
    if not tok:
        cfg = load_config()
        tok = (cfg.get("server", {}).get("access_token") or "").strip()
        if not tok:
            import secrets
            tok = secrets.token_urlsafe(24)
            update_config({"server": {"access_token": tok}})
    write_token_file(tok)
    return tok


def write_token_file(tok: str) -> None:
    """把完整令牌写入 data/access_token.txt（权限 0600），便于容器 / NAS 用户取用。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = TOKEN_FILE
        if path.exists():
            try:
                if path.read_text(encoding="utf-8").strip() == str(tok):
                    return
            except Exception:
                pass
        path.write_text(str(tok) + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
    except Exception:
        pass


def mask_token(tok: str) -> str:
    """令牌掩码（首尾各留 4 位），用于日志与远程接口展示。"""
    t = str(tok or "")
    if len(t) <= 8:
        return "*" * len(t)
    return "%s******%s" % (t[:4], t[-4:])


def outbound_verify_tls() -> bool:
    """出站 HTTPS 是否校验服务器证书（server.insecure_skip_verify 的反相）。

    默认 **True（严格校验）**。只有用户在配置里显式写了
    ``server.insecure_skip_verify = true`` 才返回 False —— 该开关是给
    「目标站证书过期 / 自签 / 本地代理做了 TLS 中间人」这类确实校验不过的场景
    留的逃生口，打开等于放弃出站连接的中间人防护，不建议长期开启。

    读取失败（配置损坏等）时一律按「校验」处理，避免静默降级。
    """
    try:
        return not bool((load_config().get("server") or {}).get("insecure_skip_verify"))
    except Exception:
        return True


def outbound_ssl_context():
    """出站请求用的 SSLContext（供 urllib 使用）。

    与 requests 的 ``verify=outbound_verify_tls()`` 共用同一开关，
    保证「图片下载 / 刮削 / 维基插件」三处出站行为一致。
    """
    import ssl

    ctx = ssl.create_default_context()
    if not outbound_verify_tls():
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


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
