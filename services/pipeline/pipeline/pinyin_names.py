"""Deterministic Chinese character-name spelling candidates.

The source surface is authority.  This module never translates meanings and never
creates identity links; it only proposes tone-free Hanyu Pinyin display spellings.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product
import re

from pypinyin import Style, pinyin


HANZI = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{2,4}$")
FOREIGN_MARKS = re.compile(r"[·•・A-Za-z]")
NICKNAME_PREFIXES = {"阿", "老", "小", "大"}
GENERIC_TITLES = {
    "掌柜", "老板", "师父", "师傅", "长老", "宗主", "门主", "院长", "族长",
    "公子", "小姐", "少爷", "夫人", "老者", "少年", "少女", "男子", "女子",
    "皇帝", "皇后", "太子", "将军", "队长", "老师", "医生", "母亲", "父亲",
}

# Longest match wins, so the compound list is checked before the single-character
# one.  Both are deliberately narrow: a surname split is only asserted where it is
# safe, never guessed to cover every name.
COMPOUND_SURNAMES = {
    "欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫", "万俟",
    "闻人", "夏侯", "诸葛", "尉迟", "公羊", "赫连", "澹台", "皇甫", "宗政",
    "濮阳", "公冶", "太叔", "申屠", "公孙", "慕容", "仲孙", "钟离", "长孙",
    "宇文", "司徒", "鲜于", "司空", "闾丘", "子车", "亓官", "司寇", "巫马",
}
# Auto-approval deliberately uses a conservative high-frequency subset.  Rare surname
# interpretations such as 水 in 水寒 are plausible candidates, not safe first-use facts.
AUTO_SURNAMES = set(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾萧田"
    "董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦傅方"
    "白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
    "凌单乐解查仇区朴"
)

# Script variants affect surname recognition, not the stored source spelling or
# pronunciation. In particular 龍 must receive the same boundary as 龙 (§0.8).
_SURNAME_VARIANTS = str.maketrans({
    "歐": "欧", "陽": "阳", "馬": "马", "東": "东", "獨": "独", "宮": "宫",
    "萬": "万", "聞": "闻", "諸": "诸", "遲": "迟", "連": "连", "臺": "台",
    "孫": "孙", "鍾": "钟", "鐘": "钟", "長": "长", "鮮": "鲜",
    "於": "于", "閭": "闾", "張": "张", "劉": "刘", "陳": "陈", "楊": "杨",
    "黃": "黄", "趙": "赵", "吳": "吴", "羅": "罗", "鄭": "郑", "謝": "谢",
    "許": "许", "韓": "韩", "馮": "冯", "鄧": "邓", "蕭": "萧", "蔣": "蒋",
    "葉": "叶", "蘇": "苏", "呂": "吕", "盧": "卢", "譚": "谭", "陸": "陆",
    "賈": "贾", "韋": "韦", "鄒": "邹", "閆": "闫", "龍": "龙",
    "賀": "贺", "顧": "顾", "龔": "龚", "錢": "钱", "嚴": "严", "湯": "汤",
    "單": "单", "樂": "乐", "區": "区", "樸": "朴",
})


@dataclass(frozen=True)
class NameCandidate:
    target_term: str
    pronunciation: tuple[str, ...]
    segmentation: str
    method: str = "pinyin"

    def as_dict(self) -> dict:
        return {
            "target_term": self.target_term,
            "pronunciation": list(self.pronunciation),
            "segmentation": self.segmentation,
            "method": self.method,
        }


@dataclass(frozen=True)
class NamePlan:
    candidates: tuple[NameCandidate, ...]
    auto_target: str | None
    reason: str
    term_role: str = "chinese_person"
    rendering_method: str = "pinyin"


def _surname_length(surface: str) -> int:
    surname = surface[:2].translate(_SURNAME_VARIANTS)
    if surname in COMPOUND_SURNAMES and len(surface) > 2:
        return 2
    if surname[:1] in AUTO_SURNAMES and len(surface) > 1:
        return 1
    return 0


def _format(parts: tuple[str, ...], surname_length: int) -> tuple[str, str]:
    if surname_length:
        surname_raw = "".join(parts[:surname_length]).lower()
        given_raw = "".join(parts[surname_length:]).lower()
        surname = surname_raw[:1].upper() + surname_raw[1:]
        given = given_raw[:1].upper() + given_raw[1:]
        return f"{surname} {given}", "surname+given"
    joined = "".join(parts).lower()
    return joined[:1].upper() + joined[1:], "mononym"


def plan_character_name(surface: str, *, max_candidates: int = 16) -> NamePlan:
    surface = surface.strip()
    if surface in GENERIC_TITLES:
        return NamePlan((), None, "generic_title")
    if not surface or FOREIGN_MARKS.search(surface):
        return NamePlan((), None, "foreign_or_mixed_name")
    if not HANZI.fullmatch(surface):
        return NamePlan((), None, "not_simple_hanzi_name")

    surname_length = _surname_length(surface)
    readings = pinyin(surface, style=Style.NORMAL, heteronym=True, errors="ignore", strict=True)
    if len(readings) != len(surface) or any(not row for row in readings):
        return NamePlan((), None, "missing_pinyin")

    candidates: list[NameCandidate] = []
    seen: set[str] = set()
    for combination in product(*(tuple(dict.fromkeys(row)) for row in readings)):
        target, segmentation = _format(combination, surname_length)
        if target not in seen:
            seen.add(target)
            candidates.append(NameCandidate(target, tuple(combination), segmentation))
        if len(candidates) >= max_candidates:
            break

    reasons = []
    if not surname_length:
        reasons.append("ambiguous_name_structure")
    if surface[0] in NICKNAME_PREFIXES:
        reasons.append("nickname_or_honorific")
    if any(len(row) > 1 for row in readings):
        reasons.append("polyphonic")
    if len(candidates) >= max_candidates:
        reasons.append("candidate_limit")
    reason = ",".join(dict.fromkeys(reasons)) or "deterministic"
    auto = candidates[0].target_term if reason == "deterministic" and len(candidates) == 1 else None
    return NamePlan(tuple(candidates), auto, reason)
