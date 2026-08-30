# -*- coding: utf-8 -*-
"""番号类型识别：判断一部片属于 FC2 / 素人 / 普通商业片。

目的：让刮削「智能化」——按番号类型跳过不可能命中的数据源，既提速又减少
无效请求（例：javbus 不收录 FC2 与素人片，av-wiki 只收录素人片）。

设计原则：
* **宁可漏判，不可误判**。误判的代价是跳过本该命中的源导致刮不到；漏判只是
  多跑一个源、慢一点，结果仍正确。因此规则保守，识别不了就返回通用类型。
* 素人厂牌表可配置扩展（``scraper.amateur_prefixes``），用户可自行补充。

类型说明：
* ``fc2``        FC2 同人（FC2-PPV-1234567）——javbus 不收录
* ``amateur``    素人片（MFC / SIRO / SONE / MIAA …）——javbus 基本不收录
* ``mainstream`` 普通商业片（SSIS / ABP / MIDE …）——av-wiki 不收录
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Set

#: FC2 番号特征
_FC2_PREFIXES = ("FC2-PPV-", "FC2-")

#: 内置素人厂牌前缀（FANZA 素人 / 个人撮影 / MGS 系常见厂牌）。
#: 素人番号与普通番号在正则上完全同形（都是「字母前缀 + 数字」），
#: 只能靠厂牌白名单区分，因此这张表是识别素人的唯一依据。
AMATEUR_PREFIXES: Set[str] = {
    # --- MGS / FANZA 素人主力 ---
    "MFC", "SIRO", "SONE", "MIAA", "ORE", "PGD", "SABA", "DOC", "SQB", "MIUM",
    "HHL", "ARA", "BKK", "ENDX", "KOSE", "SDJS", "NSPS", "ESDX", "SIMM", "MKCK",
    "TEN", "TPN", "TSK", "WES", "NNPJ", "SAN", "AKDL", "DASS", "GANA", "MTALL",
    "CND", "HUNTB", "KTDS", "YTR", "SDAB", "MUKC", "JNT", "JJM", "BLK", "CUTE",
    "GIRL", "MAN", "NKG", "RCT", "SKY", "SSB", "ZEX", "OBQ", "PKC", "NNP",
    "MAAN", "JKZ", "FCP", "AGEMIX", "AWAT", "BDSR", "CJOD", "CLUB", "DCV",
    "DIY", "EBOD", "FSET", "GTJ", "HAMY", "HND", "ICD", "IOR", "JAC", "KIRAY",
    "LCB", "LULU", "MAG", "MBYD", "MDSM", "MJHC", "MKMP", "MMK", "MNW", "MSK",
    "MUGEN", "MYBA", "NATR", "NKK", "NMDS", "NTR", "OBI", "ONS", "OSP", "PAKO",
    "PCN", "PFF", "PON", "PPB", "PTKO", "RBK", "RCTP", "RDT", "REBDB", "RKZ",
    "RMD", "RMT", "SDD", "SDMB", "SDMU", "SDNM", "SDSM", "SHOP", "SIS", "SKB",
    "SLBA", "SMJH", "SMM", "SOAV", "SOE", "SORA", "SPOS", "SWF", "TAK", "TDM",
    "TKK", "TMHP", "TNO", "TOKM", "TRSO", "UBE", "ULTR", "URKK", "VENX", "VIP",
    "WAM", "WIX", "WPW", "XAAS", "YAR", "YUM", "ZIZG",
    # --- 常见素人 / 个人撮影补充 ---
    "H4610", "TOKYO", "TMA", "LUXU", "KIRARI", "MINT", "PCOLLE", "HATOMA",
    "SCUTE", "JKSR", "MUKC", "NIPPON", "PAKO", "PRESTIGE", "SHINKI", "SOFT",
}

#: 类型常量
FC2 = "fc2"
AMATEUR = "amateur"
MAINSTREAM = "mainstream"


def _extra_prefixes(cfg: Optional[Dict[str, Any]]) -> Set[str]:
    """从配置读取用户补充的素人厂牌前缀。"""
    if not cfg:
        return set()
    raw = (cfg.get("scraper", {}) or {}).get("amateur_prefixes")
    if not raw:
        return set()
    if isinstance(raw, str):
        items: Iterable[str] = raw.replace("\n", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        return set()
    return {str(x).strip().upper() for x in items if str(x).strip()}


def split_prefix(code: str) -> str:
    """取番号前缀（'-' 之前的字母部分），如 'MFC-354' -> 'MFC'。"""
    c = (code or "").strip().upper()
    if not c:
        return ""
    head = c.split("-", 1)[0]
    # 只保留字母（剔除 259LUXU 这类数字前缀里的数字）
    letters = "".join(ch for ch in head if ch.isalpha())
    return letters


def classify(code: str, code_rule: str = "",
             cfg: Optional[Dict[str, Any]] = None) -> str:
    """判断番号类型，返回 'fc2' / 'amateur' / 'mainstream'。

    ``code_rule`` 是 parser 识别番号时命中的规则名（已存 movies.code_rule），
    用它可直接判定 FC2，最可靠；前缀表作为素人判定的补充依据。
    """
    c = (code or "").strip().upper()
    rule = (code_rule or "").strip().upper()

    # 1) FC2：规则命中，或番号本身就是 FC2-PPV-xxx
    if rule == "FC2" or c.startswith(_FC2_PREFIXES) or c.startswith("FC2PPV"):
        return FC2

    # 2) 素人：厂牌前缀命中（内置表 + 用户配置）
    prefix = split_prefix(c)
    if prefix:
        extra = _extra_prefixes(cfg)
        if prefix in AMATEUR_PREFIXES or prefix in extra:
            return AMATEUR

    # 3) 其余按普通商业片处理（保守兜底，仍会走主源）
    return MAINSTREAM


def classify_movie(movie: Dict[str, Any],
                   cfg: Optional[Dict[str, Any]] = None) -> str:
    """从影片 dict 取番号与规则名进行分类。"""
    if not movie:
        return MAINSTREAM
    return classify(movie.get("code") or "", movie.get("code_rule") or "", cfg)
