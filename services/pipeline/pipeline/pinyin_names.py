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

# Longest match wins.  The compound list plus the traditional Hundred Family
# Surnames covers ordinary novel names without asking the LLM to invent a split.
COMPOUND_SURNAMES = {
    "欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫", "万俟",
    "闻人", "夏侯", "诸葛", "尉迟", "公羊", "赫连", "澹台", "皇甫", "宗政",
    "濮阳", "公冶", "太叔", "申屠", "公孙", "慕容", "仲孙", "钟离", "长孙",
    "宇文", "司徒", "鲜于", "司空", "闾丘", "子车", "亓官", "司寇", "巫马",
}
SURNAMES = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
    "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍"
    "虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉龚程"
    "嵇邢滑裴陆荣翁荀羊甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓牧隗山"
    "谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司"
    "韶郜黎蓟薄印宿白怀蒲台从鄂索咸籍赖卓蔺屠蒙池乔阴胥能苍双闻莘党翟"
    "谭贡劳逄姬申扶堵冉宰郦雍郤璩桑桂濮牛寿通边扈燕冀郏浦尚农温别庄晏"
    "柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇"
    "广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜"
    "养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公"
)
# Auto-approval deliberately uses a conservative high-frequency subset.  Rare surname
# interpretations such as 水 in 水寒 are plausible candidates, not safe first-use facts.
AUTO_SURNAMES = set(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾萧田"
    "董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦傅方"
    "白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
    "凌单乐解查仇区朴"
)


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
    if surface[:2] in COMPOUND_SURNAMES and len(surface) > 2:
        return 2
    if surface[:1] in AUTO_SURNAMES and len(surface) > 1:
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
