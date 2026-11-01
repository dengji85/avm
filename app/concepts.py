# -*- coding: utf-8 -*-
"""检索概念 → 同义词。

为什么需要：类型/标签名来自不同数据源，同一个意思可能有中日英多种写法
（丝袜 / 絲襪 / パンスト / stockings）。用户用中文提问，库里存的可能是日文
标签，直接用一句 `LIKE '%丝袜%'` 会全军覆没。

约定：
* 只收录「确实是同一个意思」的写法，不做近义扩展（熟女 ≠ 人妻）；
* 匹配仍然是子串 LIKE（%词%），所以「パンスト」能命中「パンスト・タイツ」；
* 没收录的词原样返回，不会因为不在表里就搜不到。
"""
from __future__ import annotations

import re
from typing import Dict, List

# 用户（或模型）给的题材词常带着动词与虚词：「穿丝袜的」「那种有蕾丝的」。
# 直接拿去 LIKE 会因为多出两个字而全空，所以先剥掉这些缀词。
_LEAD = re.compile(r"^(?:那种|这种|喜欢|爱看|想看|穿|戴|有|是|来点|一些|几个)+")
_TAIL = re.compile(r"(?:的|那种|之类|一类|片|影片|片子|作品|电影)+$")

CONCEPTS: Dict[str, List[str]] = {
    "丝袜": ["丝袜", "絲襪", "裤袜", "連褲襪", "连裤袜", "パンスト", "ストッキング",
             "stocking", "pantyhose", "tights"],
    "巨乳": ["巨乳", "爆乳", "big tits", "bigtits", "large breasts"],
    "熟女": ["熟女", "年上", "高齢", "高龄", "MILF", "おばさん"],
    "人妻": ["人妻", "已婚", "主妇", "主婦", "married", "wife"],
    "学生": ["学生", "學生", "女子校生", "スクール", "schoolgirl", "校服"],
    "教师": ["教师", "教師", "女教师", "女教師", "先生", "teacher"],
    "护士": ["护士", "護士", "ナース", "看護師", "nurse"],
    "女仆": ["女仆", "女僕", "メイド", "maid"],
    "泳装": ["泳装", "泳裝", "水着", "泳衣", "比基尼", "ビキニ", "swimsuit", "bikini"],
    "和服": ["和服", "着物", "浴衣", "kimono"],
    "办公室": ["办公室", "辦公室", "OL", "职场", "キャリアウーマン", "office lady"],
    "姐妹": ["姐妹", "姊妹", "姉妹", "sisters"],
    "母亲": ["母亲", "母親", "義母", "义母", "ママ"],
    "露出": ["露出", "野外", "户外", "青姦", "outdoor"],
    "束缚": ["束缚", "束縛", "緊縛", "捆绑", "縛り", "bondage"],
    "蕾丝": ["蕾丝", "蕾絲", "レース", "lace"],
    "眼镜": ["眼镜", "眼鏡", "メガネ", "glasses"],
}

# 词 → 所属概念组（大小写不敏感）；命中任意一个写法都把整组带进查询
_INDEX: Dict[str, List[str]] = {}
for _words in CONCEPTS.values():
    for _w in _words:
        _INDEX[_w.strip().lower()] = _words


def clean(term: str) -> str:
    """剥掉动词 / 虚词前后缀：「穿丝袜的」→「丝袜」。"""
    t = str(term or "").strip()
    for _ in range(2):                      # 「那种穿蕾丝的」要剥两轮
        prev = t
        t = _LEAD.sub("", t)
        t = _TAIL.sub("", t)
        t = t.strip()
        if t == prev:
            break
    return t


# 口语年龄：「年纪大一点」「看着成熟」这类没有数字的说法。
# 阈值是经验值——用户说了具体数字时以数字为准。
_AGE_RULES = (
    (re.compile(r"年纪大|年龄大|上了年纪|年长|熟女|中年|偏大|成熟"), "min", 40),
    (re.compile(r"年纪小|年龄小|年轻|小姑娘|少女|偏小|嫩"), "max", 25),
)


def age_hint(text: str):
    """从句子里猜年龄条件，返回 (min|max, 岁) 或 None。"""
    s = str(text or "")
    for pattern, kind, value in _AGE_RULES:
        if pattern.search(s):
            return kind, value
    return None


def expand_term(term: str) -> List[str]:
    """把一个检索词展开成同义词列表；未收录的原样返回。

    先剥缀词再查表：模型给的可能是「穿丝袜」「丝袜的」这种带词缀的写法。
    """
    raw = clean(term)
    if not raw:
        return []
    words = _INDEX.get(raw.lower())
    if not words:
        return [raw]
    out: List[str] = []
    for w in words:
        if w and w not in out:
            out.append(w)
    return out


#: 别名（小写） → 规范名；与 CONCEPTS 同源，每个概念组的所有写法收敛到组名
_ALIAS: Dict[str, str] = {}
for _canon, _words in CONCEPTS.items():
    for _w in _words:
        _ALIAS.setdefault(_w.strip().lower(), _canon)


def canonical_term(term: str) -> str:
    """把类型 / 标签名收敛成规范写法；未收录的原样返回。

    与 ``expand_term`` 方向相反，两者分工：

    * ``expand_term``   —— **检索时**把「丝袜」展开成多语言写法，把查询放大；
    * ``canonical_term`` —— **入库时**把 絲襪 / パンスト / stockings 收敛成同一个名字。

    不做这一步的后果：同一题材在库里存成三行，筛选与统计被拆散——用户在「类型」
    面板里点「丝袜」只有 3 部，点「パンスト」又是另 5 部，其实是同一批片。
    """
    t = str(term or "").strip()
    if not t:
        return ""
    key = t.lower()
    hit = _ALIAS.get(key)
    if hit:
        return hit
    # 英文复数兜底：表里收的是单数（stocking），源站可能给 stockings
    if len(key) > 3 and key.endswith("s"):
        hit = _ALIAS.get(key[:-1])
        if hit:
            return hit
    return t
