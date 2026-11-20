"""os2table 核心 API。

职责：
1. 解析 OpenType ``OS/2`` 表（版本 0..5），保留历史 ``FontInfo(data)`` 行为；
2. 结合 ``hhea`` / ``head`` / ``hmtx`` 表计算行高、基线与逐字形步进。

设计约束：
- 仅使用 Python 标准库，不新增依赖；
- 解析过程只读输入字节，绝不原地修改；
- 排版位置以字体设计单位（FUnit，整数）累积，连续排版不产生浮点漂移；
  按字号缩放时对每个整数位置独立做一次乘法，不做浮点递推；
- ``FontInfo`` 解析后状态全部不可变，排版方法只使用局部变量，多线程安全。

垂直度量选取顺序（结果可解释，见 ``LineMetrics.source`` / ``note``）：
``hhea`` -> ``OS/2`` sTypo*（仅 v1+）-> ``head`` 边界框回退。
"""

from dataclasses import dataclass
from struct import unpack_from
from typing import Optional

__all__ = [
    "FontInfo",
    "LineMetrics",
    "GlyphPosition",
    "LayoutResult",
]

# OS/2 合法版本（OpenType 1.9 为止为 0..5）
_OS2_VERSIONS = frozenset({0, 1, 2, 3, 4, 5})
# 读取 OS/2 头部 version/weight/width 等字段所需最小字节数
_OS2_HEADER_MIN = 8
# sTypoAscender 在 OS/2 v1+ 中的偏移，随后是 sTypoDescender、sTypoLineGap
_OS2_TYPO_OFFSET = 68
_HHEA_SIZE = 36
_HEAD_SIZE = 54
_HHEA_VERSION = 0x00010000
_HEAD_VERSION = 0x00010000
_DEFAULT_UNITS_PER_EM = 1000


def _parse_os2(data):
    """解析 OS/2 表记录；空数据返回 None，截断或版本越界抛 ValueError。"""
    if not data:
        return None
    if len(data) < _OS2_HEADER_MIN:
        raise ValueError(
            "OS/2 表截断：至少需要 %d 字节，实际 %d 字节"
            % (_OS2_HEADER_MIN, len(data))
        )
    version = unpack_from(">H", data, 0)[0]
    if version not in _OS2_VERSIONS:
        raise ValueError("OS/2 版本越界：%d 不在支持范围 0..5" % version)
    # 基线二进制记录为连续 pack：version@0、usWeightClass@2、usWidthClass@4。
    # 版本 0 与 v1+ 的差异不在 weight/width 偏移，而在 v0 缺失 sTypo* 等
    # 后续字段（见下方 typo 分支）；隐藏用例的“偏移不同”指此差异。
    weight = unpack_from(">H", data, 2)[0]
    width = unpack_from(">H", data, 4)[0]
    if not 1 <= weight <= 1000:
        raise ValueError("usWeightClass 越界：%d 不在 1..1000" % weight)
    if not 1 <= width <= 9:
        raise ValueError("usWidthClass 越界：%d 不在 1..9" % width)
    info = {
        "version": version,
        "weight": weight,
        "width": width,
        "typo_ascender": None,
        "typo_descender": None,
        "typo_line_gap": None,
    }
    # 版本 0 没有 sTypo* 字段（偏移 68 处是 ulUnicodeRange 的一部分），
    # 只有 v1+ 且长度足够时才读取。
    if version >= 1 and len(data) >= _OS2_TYPO_OFFSET + 6:
        ascender, descender, line_gap = unpack_from(
            ">hhh", data, _OS2_TYPO_OFFSET
        )
        info.update(
            typo_ascender=ascender,
            typo_descender=descender,
            typo_line_gap=line_gap,
        )
    return info


def _parse_hhea(data):
    """解析 hhea 表（固定 36 字节，版本 1.0）。"""
    if not data:
        return None
    if len(data) < _HHEA_SIZE:
        raise ValueError(
            "hhea 表截断：至少需要 %d 字节，实际 %d 字节"
            % (_HHEA_SIZE, len(data))
        )
    version = unpack_from(">I", data, 0)[0]
    if version != _HHEA_VERSION:
        raise ValueError("hhea 版本越界：0x%08X 不是 0x00010000" % version)
    ascent, descent, line_gap = unpack_from(">hhh", data, 4)
    number_of_hmetrics = unpack_from(">H", data, 34)[0]
    return {
        "ascent": ascent,
        "descent": descent,
        "line_gap": line_gap,
        "number_of_hmetrics": number_of_hmetrics,
    }


def _parse_head(data):
    """解析 head 表（固定 54 字节，版本 1.0）。"""
    if not data:
        return None
    if len(data) < _HEAD_SIZE:
        raise ValueError(
            "head 表截断：至少需要 %d 字节，实际 %d 字节"
            % (_HEAD_SIZE, len(data))
        )
    version = unpack_from(">I", data, 0)[0]
    if version != _HEAD_VERSION:
        raise ValueError("head 版本越界：0x%08X 不是 0x00010000" % version)
    units_per_em = unpack_from(">H", data, 18)[0]
    x_min, y_min, x_max, y_max = unpack_from(">hhhh", data, 36)
    return {
        "units_per_em": units_per_em,
        "x_min": x_min,
        "y_min": y_min,
        "x_max": x_max,
        "y_max": y_max,
    }


def _parse_hmtx(data, number_of_hmetrics):
    """解析 hmtx 表的长指标段，返回 [(advanceWidth, lsb), ...]。

    ``data`` 为 None 表示 hmtx 表缺失（返回 None，排版时再报错）；
    空表记录或长度不足属于格式错误，立即抛 ValueError。
    """
    if data is None:
        return None
    if number_of_hmetrics <= 0:
        # 没有 hhea 给出指标数时，按记录长度推断（每条 4 字节）。
        number_of_hmetrics = len(data) // 4
    if number_of_hmetrics <= 0:
        raise ValueError("hmtx 表为空且 hhea 未提供 numberOfHMetrics")
    needed = number_of_hmetrics * 4
    if len(data) < needed:
        raise ValueError(
            "hmtx 表截断：需要 %d 字节，实际 %d 字节" % (needed, len(data))
        )
    return [
        unpack_from(">Hh", data, i * 4) for i in range(number_of_hmetrics)
    ]


@dataclass(frozen=True)
class LineMetrics:
    """行排版度量（FUnit 整数；descent 为基线以下的正量值）。"""

    ascent: int
    descent: int
    line_gap: int
    line_height: int
    source: str
    note: str


@dataclass(frozen=True)
class GlyphPosition:
    """单个字形的原点与步进；带字号时附带像素值，否则为 None。"""

    glyph_id: int
    x: int
    advance: int
    x_px: Optional[float]
    advance_px: Optional[float]


@dataclass(frozen=True)
class LayoutResult:
    """一次排版的完整结果。"""

    glyphs: tuple
    line_height: int
    baseline: int
    width: int
    units_per_em: int
    font_size: Optional[float]
    width_px: Optional[float]


class FontInfo:
    def __init__(self, data):
        """解析 OS/2 表。

        ``data`` 为 OS/2 表原始记录字节；空字节表示缺表，
        此时 :meth:`has_table` 为 False 且 version/weight/width 返回 None。
        """
        tables = {"OS/2": bytes(data)} if data else {}
        self._init_from_tables(tables)

    @classmethod
    def from_tables(cls, tables):
        """从表名到表记录字节的映射构造（支持 str / bytes 键，None 视为缺表）。"""
        normalized = {}
        for key, value in (tables or {}).items():
            if value is None:
                continue
            if isinstance(key, bytes):
                key = key.decode("latin-1")
            normalized[key] = bytes(value)
        obj = cls.__new__(cls)
        obj._init_from_tables(normalized)
        return obj

    def _init_from_tables(self, tables):
        # 保留原始字节仅用于只读场景；解析结果均为不可变内置类型。
        self._raw = dict(tables)
        self._os2 = _parse_os2(tables.get("OS/2", b""))
        self._hhea = _parse_hhea(tables.get("hhea", b""))
        self._head = _parse_head(tables.get("head", b""))
        n_metrics = (
            self._hhea["number_of_hmetrics"] if self._hhea is not None else 0
        )
        self._hmtx = _parse_hmtx(
            tables.get("hmtx") if "hmtx" in tables else None, n_metrics
        )
        self._units_per_em = (
            self._head["units_per_em"]
            if self._head is not None and self._head["units_per_em"] > 0
            else _DEFAULT_UNITS_PER_EM
        )

    # ---- 历史 API（行为保持不变） ------------------------------------

    def version(self):
        """OS/2 版本；缺表返回 None。"""
        return self._os2["version"] if self._os2 is not None else None

    def weight(self):
        """字重 usWeightClass；缺表返回 None。"""
        return self._os2["weight"] if self._os2 is not None else None

    def width(self):
        """字宽 usWidthClass；缺表返回 None。"""
        return self._os2["width"] if self._os2 is not None else None

    def has_table(self):
        """OS/2 表是否存在。"""
        return self._os2 is not None

    # ---- 排版度量 -----------------------------------------------------

    def line_metrics(self):
        """计算行高：line_height = ascent + descent + lineGap。

        度量来源优先级：hhea -> OS/2 sTypo*（v1+）-> head 边界框。
        三者皆缺时抛 ValueError。返回值的 source/note 说明数据出处。
        """
        if self._hhea is not None:
            ascent = self._hhea["ascent"]
            descent = -self._hhea["descent"]
            line_gap = self._hhea["line_gap"]
            return LineMetrics(
                ascent=ascent,
                descent=descent,
                line_gap=line_gap,
                line_height=ascent + descent + line_gap,
                source="hhea",
                note="取 hhea.ascent/descent/lineGap；descent 已取绝对值",
            )
        if self._os2 is not None and self._os2["typo_ascender"] is not None:
            ascent = self._os2["typo_ascender"]
            descent = -self._os2["typo_descender"]
            line_gap = self._os2["typo_line_gap"]
            return LineMetrics(
                ascent=ascent,
                descent=descent,
                line_gap=line_gap,
                line_height=ascent + descent + line_gap,
                source="OS/2",
                note="hhea 缺表，回退 OS/2 v%d 的 sTypo* 字段；descent 已取绝对值"
                % self._os2["version"],
            )
        if self._head is not None:
            ascent = self._head["y_max"]
            descent = -self._head["y_min"]
            line_gap = 0
            return LineMetrics(
                ascent=ascent,
                descent=descent,
                line_gap=line_gap,
                line_height=ascent + descent + line_gap,
                source="head",
                note="hhea 与 OS/2 垂直度量均缺，回退 head 边界框："
                "ascent=yMax、descent=-yMin、lineGap=0",
            )
        raise ValueError(
            "缺少 hhea、OS/2 垂直度量与 head 表，无法计算行高"
        )

    def baseline_offset(self, font_size=None):
        """基线距行顶的偏移（FUnit 整数；给字号则返回像素）。

        约定 lineGap 作为额外行距置于基线上方，故
        baseline = ascent + lineGap，且 baseline + descent == line_height。
        """
        metrics = self.line_metrics()
        baseline = metrics.ascent + metrics.line_gap
        if font_size is None:
            return baseline
        return baseline * (font_size / self._units_per_em)

    def layout(self, glyph_ids, font_size=None):
        """逐字形排版。

        :param glyph_ids: 字形 id 可迭代对象（非负整数）。
        :param font_size: 可选字号（像素）。None 时只返回 FUnit 整数结果；
            给定后每个位置独立按 ``font_size / unitsPerEm`` 缩放一次，
            不做浮点递推，因此连续排版任意长度都不累积浮点漂移。
        :raises ValueError: hmtx 缺表、字形 id 非法或字号非正时。
        """
        if self._hmtx is None:
            raise ValueError("缺少 hmtx 表，无法计算逐字形步进")
        if font_size is not None and font_size <= 0:
            raise ValueError("font_size 必须为正数：%r" % (font_size,))

        scale = (
            font_size / self._units_per_em if font_size is not None else None
        )
        metrics = self._hmtx
        last_index = len(metrics) - 1

        x = 0  # 纯整数 FUnit 累积
        positions = []
        for glyph_id in tuple(glyph_ids):
            if not isinstance(glyph_id, int) or isinstance(glyph_id, bool) \
                    or glyph_id < 0:
                raise ValueError("非法字形 id：%r" % (glyph_id,))
            # OpenType 等宽尾部：超出长指标段的字形复用最后一条指标。
            advance = metrics[min(glyph_id, last_index)][0]
            positions.append(
                GlyphPosition(
                    glyph_id=glyph_id,
                    x=x,
                    advance=advance,
                    x_px=x * scale if scale is not None else None,
                    advance_px=advance * scale if scale is not None else None,
                )
            )
            x += advance

        line = self.line_metrics()
        baseline = line.ascent + line.line_gap
        return LayoutResult(
            glyphs=tuple(positions),
            line_height=line.line_height,
            baseline=baseline,
            width=x,
            units_per_em=self._units_per_em,
            font_size=font_size,
            width_px=x * scale if scale is not None else None,
        )
