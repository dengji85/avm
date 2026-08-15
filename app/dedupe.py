import os
from typing import Any, Dict, List, Optional

from .db import connect, query_all, query_one


def _file_rows(conn):
    sql = (
        "SELECT f.id, f.path, f.size, f.mtime, f.part, f.movie_id, f.quick_hash, "
        "m.code, m.title, m.resolution "
        "FROM movie_files f JOIN movies m ON m.id = f.movie_id "
        "WHERE f.missing = 0"
    )
    return query_all(conn, sql)


def find_exact_duplicates(conn) -> List[Dict[str, Any]]:
    """按 (size, quick_hash) 归组，识别内容完全相同的文件（跨目录/跨命名）。"""
    groups: Dict[tuple, List[dict]] = {}
    for r in _file_rows(conn):
        if not r["quick_hash"]:
            continue  # 未计算指纹的无法判定
        groups.setdefault((r["size"], r["quick_hash"]), []).append(dict(r))
    return [
        {"kind": "exact", "size": sz, "hash": h, "files": files}
        for (sz, h), files in groups.items() if len(files) > 1
    ]


def find_version_groups(conn) -> List[Dict[str, Any]]:
    """按番号归组，识别同一作品的不同版本（不同分辨率/压制/字幕/分片）。

    若同番号下的多个文件其实是分卷（xxx_1/xxx_2 这类去序号后基础名相同），
    则标记 multi_part=True，整组不计入版本冗余、也不参与劣质/损坏误判。
    """
    by_movie: Dict[int, List[dict]] = {}
    for r in _file_rows(conn):
        by_movie.setdefault(r["movie_id"], []).append(dict(r))
    groups = []
    for mid, files in by_movie.items():
        if len(files) < 2:
            continue
        group_mp = _mark_multi_part(files)
        groups.append({"kind": "version", "movie_id": mid, "code": files[0]["code"],
                       "title": files[0]["title"], "files": files, "multi_part": group_mp})
    return groups


def pick_best(files: List[dict]) -> dict:
    """在同番号多版本里挑选最佳版：分辨率最高，其次文件最大。"""
    order = {"": 0, "480p": 1, "720p": 2, "1080p": 3, "1440p": 4, "2160p": 5}

    def score(f):
        return (order.get(f.get("resolution") or "", 0), f.get("size") or 0)

    return max(files, key=score)


def resolve_group(conn, kind: str, keep_file_id: int, delete_files: bool = False) -> int:
    """保留 keep_file_id 对应文件，删除同组其余文件（标记 missing，或物理删除）。
    返回被移除的文件数。"""
    row = query_one(conn,
                    "SELECT f.*, m.movie_id AS mid FROM movie_files f "
                    "JOIN movies m ON m.id = f.movie_id WHERE f.id = ?",
                    (keep_file_id,))
    if not row:
        return 0
    if kind == "exact":
        others = query_all(conn,
                           "SELECT * FROM movie_files WHERE size = ? AND quick_hash = ? "
                           "AND id <> ? AND missing = 0",
                           (row["size"], row["quick_hash"], keep_file_id))
    else:  # version
        others = query_all(conn,
                           "SELECT * FROM movie_files WHERE movie_id = ? AND id <> ? AND missing = 0",
                           (row["movie_id"], keep_file_id))
    removed = 0
    for o in others:
        if delete_files:
            try:
                os.remove(o["path"])
            except OSError:
                pass
        conn.execute("UPDATE movie_files SET missing = 1 WHERE id = ?", (o["id"],))
        removed += 1
    conn.commit()
    return removed


def scan(conn) -> Dict[str, Any]:
    """一次性返回精确重复组 + 版本组，供前端展示。"""
    exact = find_exact_duplicates(conn)
    version = find_version_groups(conn)
    exact_files = sum(len(g["files"]) - 1 for g in exact)
    # 分卷组（multi_part）是合法多文件，不计入版本冗余
    version_files = sum(len(g["files"]) - 1 for g in version if not g.get("multi_part"))
    return {
        "exact": exact,
        "version": version,
        "exact_groups": len(exact),
        "version_groups": len(version),
        "exact_redundant": exact_files,
        "version_redundant": version_files,
    }


# ------------------------------------------------------------------ 劣质片智能筛查
#
# 纯启发式，不解析视频流：基于文件名标记、分辨率档位、相对体积、同番号多版本比较。
# 资深收藏者常见痛点：片头广告/推销样片、压得过于模糊的低码率片、
# 同番号重复下载的劣质版本、损坏/不完整的坏档。

# 广告 / 推销 / 预览样片 文件名特征（匹配 basename，不分大小写）
_AD_KEYWORDS = (
    r"sample", r"trailer", r"preview", r"advert", r"广告", r"预告", r"宣传",
    r"片段", r"\bshort\b", r"teaser", r"demo", r"nip", r"\\bpr\b",
    r"promo", r"\\bcm\b", r"fans? ?meet", r"メイキング", r"making",
)
import re as _re
_AD_RE = _re.compile("|".join(f"(?:{p})" for p in _AD_KEYWORDS), _re.I)

# 分卷/分片识别：同一影片被切成多段保存，文件名形如
#   xxx_1 / xxx_2          （下划线 + 序号）
#   xxx-cd1 / xxx-CD2      （cd/part/disk/vol + 序号）
#   xxx.part3 / xxx.part03 （part + 序号）
#   xxx (1).mp4 / (2)      （括号序号）
#   xxx-a / xxx-b          （字母序号）
# 这类多文件是「合法分卷」，不应算作同番号重复版本，也不应被当成劣质/损坏误删。
# 仅当组内多个文件去掉末尾序号后缀后「基础名相同」且序号不同，才判定为分卷。

# 各种分段序号后缀（匹配末尾，区分大小写由调用处统一 lower）
_PART_PATTERNS = [
    r"[-_.\s]?\((\d+)\)",          # (1) / (01)
    r"[-_.\s]?\[(\d+)\]",          # [2]
    r"[-_.\s]?(?:cd|part|disk|vol|dvd)[-_.\s]?(\d+)",  # cd1 / part03 / disk 2
    r"[-_.\s]([a-z])\b",           # -a / _b（单字母分卷，需前置分隔符避免误吞番号末位）
    r"[-_.\s](\d+)$",              # 末尾纯数字序号：_1 / -2 / 空格3（必须锚定行尾）
]
_PART_RES = [_re.compile(p, _re.I) for p in _PART_PATTERNS]


def _split_part(stem_name: str):
    """把文件名主干拆成 (基础名, 序号)。无序号返回 (原名, None)。

    序号可以是数字或单字母（a/b/c…）。基础名会去掉末尾残留的分隔符
    （_ - . 空格），避免 'ABC-123_' 这类尾巴。

    重要防误判：番号本身形如 ABC-123（字母+数字），其末尾数字 123 也会被
    '[-_.\s](\d+)$' 命中。若拆分后基础名里已不含任何数字（如 ABC），说明命中的
    其实是番号末位而非分卷序号，此时回退为「无序号」，避免把番号末位当成分段。
    """
    s = (stem_name or "").strip()
    if not s:
        return ("", None)
    for rx in _PART_RES:
        m = rx.search(s)
        if m:
            num = m.group(1)
            base = s[: m.start()].rstrip("_-. ")
            if num.isdigit() and not any(ch.isdigit() for ch in base):
                # 拆分后基础名无数字 → 命中的是番号末位，不是分卷
                return (s, None)
            key = num.lower() if num.isalpha() else int(num)
            return (base, key)
    return (s, None)


def _mark_multi_part(files: List[dict]) -> bool:
    """就地给 files 标注 multi_part。返回整组是否全部为分卷。

    规则：按「去序号后的基础名」聚合，若某基础名下出现 ≥2 个不同序号，
    则这些文件属于同一影片的分卷集合（明显分段，不算重复版本）。

    额外保护：当组内所有文件基础名都相同、但只有一个（或零个）带序号时，
    视为「同一基础名的不同文件」而非分卷——避免把 'ABC-123.mp4' 与
    'ABC-123-CD1.mp4' 这种「整片 + 单分卷」误判为分卷而漏报真实重复。
    """
    bases: Dict[str, set] = {}
    has_part = False
    for f in files:
        stem = os.path.splitext(f.get("filename") or "")[0]
        base, num = _split_part(stem)
        f["_base"] = base
        if num is not None:
            has_part = True
            bases.setdefault(base, set()).add(num)
    mp_ids = set()
    for base, nums in bases.items():
        if len(nums) >= 2:  # 同一基础名、多个不同序号 → 分卷
            for f in files:
                if f.get("_base") == base:
                    mp_ids.add(f["id"])
    for f in files:
        f["multi_part"] = f["id"] in mp_ids
    return bool(mp_ids) and len(mp_ids) == len(files) and has_part

# 分辨率档位 → 权重（用于跨档位质量比较）
_RES_ORDER = {"": 0, "480p": 1, "720p": 2, "1080p": 3, "1440p": 4, "2160p": 5}

# 同番号多版本里，一个版本体积低于「组内最大体积」的比例阈值，低于则判为劣质
_VERSION_SIZE_RATIO = 0.6
# 同分辨率档位下，体积低于该档位中位数的比例阈值，判为低码率/压制过度
_LOW_BITRATE_RATIO = 0.55


def _movie_rows(conn):
    """取出用于质量评估的影片与文件信息。"""
    sql = (
        "SELECT m.id AS mid, m.code, m.title, m.resolution, m.size AS msize, "
        "m.subtitle, m.uncensored, m.file_count, "
        "f.id AS fid, f.path, f.filename, f.size AS fsize, f.missing, f.quick_hash "
        "FROM movies m JOIN movie_files f ON f.movie_id = m.id "
        "WHERE f.missing = 0"
    )
    return query_all(conn, sql)


def find_ad_files(conn) -> List[Dict[str, Any]]:
    """文件名含广告/推销/预览特征的文件（样片、预告、宣传片段）。"""
    out = []
    for r in _movie_rows(conn):
        fn = r["filename"] or ""
        if _AD_RE.search(fn):
            out.append({
                "movie_id": r["mid"], "file_id": r["fid"], "code": r["code"],
                "title": r["title"], "path": r["path"], "size": r["fsize"] or 0,
                "resolution": r["resolution"] or "",
            })
    return out


def find_low_bitrate(conn) -> List[Dict[str, Any]]:
    """同分辨率档位内体积明显偏小的片（低码率 / 压制过度）。

    仅在分辨率已知（非空）时比较：分辨率未知无法公平评估码率，避免误报。
    """
    by_res: Dict[str, List[dict]] = {}
    for r in _movie_rows(conn):
        res = (r["resolution"] or "").strip()
        if not res or res not in _RES_ORDER or res == "":
            continue  # 跳过未知分辨率
        by_res.setdefault(res, []).append({
            "movie_id": r["mid"], "file_id": r["fid"], "code": r["code"],
            "title": r["title"], "path": r["path"], "size": r["fsize"] or 0,
            "resolution": res,
        })
    out = []
    for res, items in by_res.items():
        if len(items) < 5:
            continue  # 同档位样本太少，避免误判
        sizes = sorted(x["size"] for x in items if x["size"] > 0)
        if not sizes:
            continue
        median = sizes[len(sizes) // 2]
        for it in items:
            if it["size"] > 0 and it["size"] < median * _LOW_BITRATE_RATIO:
                out.append(it)
    return out


def find_version_losers(conn) -> List[Dict[str, Any]]:
    """同番号多版本里，体积明显低于同组最优版的劣质版本（可由 dedupe.pick_best 选出保留项）。"""
    out = []
    for g in find_version_groups(conn):
        files = g["files"]
        if len(files) < 2:
            continue
        if g.get("multi_part"):  # 分卷：单卷体积小是正常的，不判劣质
            continue
        best = pick_best(files)
        best_size = best.get("size") or 0
        if best_size <= 0:
            continue
        for f in files:
            if f["id"] == best["id"]:
                continue
            sz = f.get("size") or 0
            if sz > 0 and sz < best_size * _VERSION_SIZE_RATIO:
                out.append({
                    "movie_id": g["movie_id"], "file_id": f["id"],
                    "code": g["code"], "title": g["title"],
                    "path": f["path"], "size": sz,
                    "resolution": f.get("resolution") or "",
                    "keep_file_id": best["id"], "keep_size": best_size,
                })
    return out


def find_broken(conn) -> List[Dict[str, Any]]:
    """疑似损坏/不完整的片：分片标记为缺失，或同番号多版本里体积异常小（< 最优版 30%）。"""
    out = []
    # 1) 数据库标记为 missing 的文件（磁盘已消失但 movie 仍在）
    miss = query_all(conn,
                     "SELECT f.id AS fid, f.path, f.size, f.movie_id AS mid, "
                     "m.code, m.title FROM movie_files f "
                     "JOIN movies m ON m.id = f.movie_id WHERE f.missing = 1")
    for r in miss:
        out.append({
            "movie_id": r["mid"], "file_id": r["fid"], "code": r["code"],
            "title": r["title"], "path": r["path"], "size": r["size"] or 0,
            "resolution": "", "reason": "磁盘文件缺失",
        })
    # 2) 同番号多版本里体积极小的（损坏/不完整坏档）
    for g in find_version_groups(conn):
        files = g["files"]
        if len(files) < 2:
            continue
        if g.get("multi_part"):  # 分卷：单卷体积小是正常的，不判损坏
            continue
        best = pick_best(files)
        best_size = best.get("size") or 0
        if best_size <= 0:
            continue
        for f in files:
            if f["id"] == best["id"]:
                continue
            sz = f.get("size") or 0
            if 0 < sz < best_size * 0.3:
                out.append({
                    "movie_id": g["movie_id"], "file_id": f["id"],
                    "code": g["code"], "title": g["title"],
                    "path": f["path"], "size": sz,
                    "resolution": f.get("resolution") or "",
                    "reason": "体积远小于同番号最优版，疑似损坏/不完整",
                    "keep_file_id": best["id"], "keep_size": best_size,
                })
    return out


def scan_quality(conn) -> Dict[str, Any]:
    """返回四类劣质片筛查结果。"""
    ad = find_ad_files(conn)
    low = find_low_bitrate(conn)
    ver = find_version_losers(conn)
    broken = find_broken(conn)
    # 去重计数：同一 file_id 可能被多类命中（如既是广告片又是低码率）
    seen = set()
    flagged = 0
    for it in (*ad, *low, *ver, *broken):
        if it["file_id"] not in seen:
            seen.add(it["file_id"])
            flagged += 1
    return {
        "ad": ad, "low_bitrate": low, "version_loser": ver, "broken": broken,
        "counts": {
            "ad": len(ad), "low_bitrate": len(low),
            "version_loser": len(ver), "broken": len(broken),
            "total_flagged": flagged,
        },
    }

