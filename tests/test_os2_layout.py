"""OS/2 + hhea/head/hmtx 排版功能测试（行高、基线、逐字形步进）。

文件名含 ``os2``，可被 ``pytest -k os2`` 选中；不改动既有测试。
"""

import struct
from struct import pack, pack_into

import pytest

import os2table
from os2table import FontInfo


# ---- 二进制表构造工具 ----------------------------------------------------

def os2_record(version=4, weight=700, width=5, typo=None, size=86):
    data = bytearray(size)
    # 与基线记录布局保持一致：version@0、weight@2、width@4（连续 pack）。
    # v0 与 v1+ 的差异是后续 sTypo* 等字段的有无，不在前 6 字节。
    pack_into(">H", data, 0, version)
    pack_into(">H", data, 2, weight)
    pack_into(">H", data, 4, width)
    if typo is not None:
        asc, desc, gap = typo
        pack_into(">hhh", data, 68, asc, desc, gap)
    return bytes(data)


def hhea_record(ascent=800, descent=-200, line_gap=50, n_metrics=3):
    data = bytearray(36)
    pack_into(">I", data, 0, 0x00010000)
    pack_into(">hhh", data, 4, ascent, descent, line_gap)
    pack_into(">H", data, 34, n_metrics)
    return bytes(data)


def head_record(upem=1000, x_min=0, y_min=-250, x_max=500, y_max=800):
    data = bytearray(54)
    pack_into(">I", data, 0, 0x00010000)
    pack_into(">H", data, 18, upem)
    pack_into(">hhhh", data, 36, x_min, y_min, x_max, y_max)
    return bytes(data)


def hmtx_record(metrics):
    return b"".join(pack(">Hh", adv, lsb) for adv, lsb in metrics)


def make_font():
    return FontInfo.from_tables(
        {
            "OS/2": os2_record(typo=(900, -300, 100)),
            "hhea": hhea_record(n_metrics=3),
            "head": head_record(),
            "hmtx": hmtx_record([(500, 0), (600, 0), (700, 0)]),
        }
    )


# ---- 行高：ascent + descent + lineGap ------------------------------------

def test_os2_line_height_equals_ascent_plus_descent_plus_gap():
    f = make_font()
    m = f.line_metrics()
    assert (m.ascent, m.descent, m.line_gap) == (800, 200, 50)
    assert m.line_height == 800 + 200 + 50 == 1050
    assert m.source == "hhea"
    # hhea 优先于 OS/2 typo 度量
    assert m.line_height != 900 + 300 + 100


def test_os2_line_height_fallback_to_typo_when_hhea_missing():
    f = FontInfo.from_tables(
        {
            "OS/2": os2_record(version=1, typo=(900, -300, 100)),
            "head": head_record(),
        }
    )
    m = f.line_metrics()
    assert m.source == "OS/2"
    assert (m.ascent, m.descent, m.line_gap, m.line_height) == (
        900,
        300,
        100,
        1300,
    )
    assert "sTypo" in m.note and "回退" in m.note


def test_os2_line_height_fallback_to_head_is_explainable():
    f = FontInfo.from_tables(
        {"OS/2": os2_record(version=0), "head": head_record()}
    )
    m = f.line_metrics()
    assert m.source == "head"
    assert m.ascent == 800  # yMax
    assert m.descent == 250  # -yMin
    assert m.line_gap == 0
    assert m.line_height == 1050
    assert "yMax" in m.note and "yMin" in m.note


def test_os2_no_metrics_anywhere_raises():
    with pytest.raises(ValueError):
        FontInfo.from_tables({}).line_metrics()


# ---- 版本与字段偏移 -------------------------------------------------------

def test_os2_v0_ignores_typo_fields_v1_reads_them():
    # 同一段落在 68 偏移写入 typo 值：v0 无该字段 -> head 回退；
    # v1 有该字段 -> OS/2 typo 度量。
    v0 = FontInfo.from_tables(
        {
            "OS/2": os2_record(version=0, typo=(900, -300, 100)),
            "head": head_record(),
        }
    )
    v1 = FontInfo.from_tables(
        {
            "OS/2": os2_record(version=1, typo=(900, -300, 100)),
            "head": head_record(),
        }
    )
    assert v0.line_metrics().source == "head"
    assert v1.line_metrics().source == "OS/2"
    assert v0.version() == 0 and v1.version() == 1


@pytest.mark.parametrize("bad_version", [6, 99, 0xFFFF])
def test_os2_version_out_of_range_raises(bad_version):
    with pytest.raises(ValueError):
        FontInfo(os2_record(version=bad_version))
    with pytest.raises(ValueError):
        FontInfo.from_tables({"OS/2": os2_record(version=bad_version)})


def test_os2_all_known_versions_accepted():
    for version in range(6):
        f = FontInfo.from_tables({"OS/2": os2_record(version=version)})
        assert f.version() == version


def test_os2_hhea_and_head_bad_version_raise():
    bad_hhea = bytearray(hhea_record())
    struct.pack_into(">I", bad_hhea, 0, 0x00020000)
    with pytest.raises(ValueError):
        FontInfo.from_tables(
            {"hhea": bytes(bad_hhea), "hmtx": hmtx_record([(1, 0)])}
        )

    bad_head = bytearray(head_record())
    struct.pack_into(">I", bad_head, 0, 0x00020000)
    with pytest.raises(ValueError):
        FontInfo.from_tables({"head": bytes(bad_head)})


def test_os2_truncated_table_raises():
    with pytest.raises(ValueError):
        FontInfo(b"\x00\x04")
    with pytest.raises(ValueError):
        FontInfo.from_tables({"hhea": b"\x00" * 10})


# ---- 字重/字宽越界（隐藏边界） -------------------------------------------

@pytest.mark.parametrize("weight", [0, 1001, 65535])
def test_os2_illegal_weight_raises(weight):
    with pytest.raises(ValueError):
        FontInfo(os2_record(weight=weight))


@pytest.mark.parametrize("width", [0, 10])
def test_os2_illegal_width_raises(width):
    with pytest.raises(ValueError):
        FontInfo(os2_record(width=width))


def test_os2_eight_byte_record_is_minimum_complete_header():
    # 8 字节（_OS2_HEADER_MIN）恰好覆盖 version/weight/width，可完整解析。
    minimum = bytearray(8)
    pack_into(">H", minimum, 0, 4)
    pack_into(">H", minimum, 2, 700)
    pack_into(">H", minimum, 4, 5)
    f = FontInfo(bytes(minimum))
    assert (f.version(), f.weight(), f.width()) == (4, 700, 5)
    # 不足 8 字节按截断处理
    with pytest.raises(ValueError):
        FontInfo(bytes(minimum[:7]))

def test_os2_missing_table_weight_is_none():
    f = FontInfo(b"")
    assert f.has_table() is False
    assert f.weight() is None
    assert f.width() is None
    assert f.version() is None


# ---- 逐字形步进 -----------------------------------------------------------

def test_os2_layout_integer_positions_and_advances():
    f = make_font()
    r = f.layout([0, 1, 2])
    assert [g.glyph_id for g in r.glyphs] == [0, 1, 2]
    assert [g.x for g in r.glyphs] == [0, 500, 1100]
    assert [g.advance for g in r.glyphs] == [500, 600, 700]
    assert r.width == 1800
    assert r.glyphs[0].x_px is None  # 未给字号时纯整数结果
    # 基线：lineGap 置于基线上方，baseline + descent == line_height
    assert r.baseline == 800 + 50 == 850
    assert r.baseline + f.line_metrics().descent == r.line_height
    assert f.baseline_offset() == 850


def test_os2_layout_monospace_tail_reuses_last_metric():
    f = make_font()
    r = f.layout([99])
    assert r.glyphs[0].advance == 700  # 超出长指标段复用最后一条


def test_os2_layout_10000_glyphs_no_integer_drift():
    f = make_font()
    ids = [i % 3 for i in range(10000)]
    r = f.layout(ids)
    assert len(r.glyphs) == 10000
    # 循环 (500,600,700) 每 3 个步进 1800；10000 = 3333*3 + 1，
    # 故总宽 3333*1800 + 500 = 5999900（整数精确）。
    expected_width = 10000 // 3 * 1800 + (500 if 10000 % 3 else 0)
    assert r.width == expected_width == 5999900
    expected_x = 0
    for g in r.glyphs:
        assert g.x == expected_x  # 整数累积，逐点精确，无任何漂移
        expected_x += g.advance
    assert expected_x == r.width


def test_os2_layout_10000_glyphs_scaled_error_within_1e_9():
    f = make_font()
    font_size = 12.5
    r = f.layout([i % 3 for i in range(10000)], font_size=font_size)
    scale = font_size / 1000
    # 每个像素位置都由整数位置独立缩放一次得到（无浮点递推）
    for g in r.glyphs:
        assert abs(g.x_px - g.x * scale) <= 1e-9
        assert abs(g.advance_px - g.advance * scale) <= 1e-9
    assert abs(r.width_px - r.width * scale) <= 1e-9
    # 与独立浮点递推参照结果对比，末端误差同样受控
    ref_x = 0.0
    for g in r.glyphs:
        assert abs(g.x_px - ref_x) <= 1e-9
        ref_x += g.advance_px


def test_os2_layout_uses_upem_from_head_for_scaling():
    f = FontInfo.from_tables(
        {
            "OS/2": os2_record(),
            "hhea": hhea_record(n_metrics=1),
            "head": head_record(upem=2048),
            "hmtx": hmtx_record([(500, 0)]),
        }
    )
    r = f.layout([0], font_size=10.0)
    assert r.units_per_em == 2048
    assert r.glyphs[0].advance_px == 500 * 10.0 / 2048
    assert f.baseline_offset(10.0) == 850 * 10.0 / 2048


def test_os2_layout_without_hmtx_raises():
    f = FontInfo.from_tables(
        {"OS/2": os2_record(), "hhea": hhea_record(n_metrics=0)}
    )
    with pytest.raises(ValueError):
        f.layout([0])


def test_os2_layout_hmtx_truncated_raises():
    # hhea 声明 3 条长指标但 hmtx 只够 1 条：构造期即判格式错误。
    with pytest.raises(ValueError):
        FontInfo.from_tables(
            {
                "hhea": hhea_record(n_metrics=3),
                "hmtx": hmtx_record([(500, 0)]),
            }
        )


@pytest.mark.parametrize("bad_gid", [-1, 1.5, "x"])
def test_os2_layout_bad_glyph_id_raises(bad_gid):
    f = make_font()
    with pytest.raises(ValueError):
        f.layout([bad_gid])


def test_os2_layout_bad_font_size_raises():
    f = make_font()
    with pytest.raises(ValueError):
        f.layout([0], font_size=0)
    with pytest.raises(ValueError):
        f.layout([0], font_size=-3)


# ---- 并发安全与不可变 -----------------------------------------------------

def test_os2_concurrent_layout_same_font_is_thread_safe():
    import threading

    f = make_font()
    errors = []
    results = []
    lock = threading.Lock()

    def worker():
        try:
            for _ in range(50):
                r1 = f.layout([i % 3 for i in range(1000)])
                r2 = f.layout(
                    [i % 3 for i in range(1000)], font_size=17.0
                )
                with lock:
                    results.append(
                        (r1.width, r2.width_px, f.weight(), f.version())
                    )
        except Exception as exc:  # pragma: no cover - 失败路径
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(results) == 8 * 50
    assert len(set(results)) == 1  # 所有线程结果完全一致


def test_os2_input_bytes_are_not_mutated():
    raw = os2_record()
    snapshot = bytes(raw)
    FontInfo(raw).weight()
    assert raw == snapshot

    tables = {
        "OS/2": os2_record(),
        "hhea": hhea_record(n_metrics=1),
        "hmtx": hmtx_record([(500, 0)]),
    }
    snapshots = {k: bytes(v) for k, v in tables.items()}
    f = FontInfo.from_tables(tables)
    f.layout([0, 0])
    for key, value in tables.items():
        assert value == snapshots[key]


def test_os2_from_tables_accepts_bytes_keys_and_none():
    f = FontInfo.from_tables(
        {b"OS/2": os2_record(version=0), "head": head_record(), "nope": None}
    )
    assert f.version() == 0
    assert f.line_metrics().source == "head"


# ---- 既有解析行为不变 -----------------------------------------------------

def test_os2_legacy_fontinfo_behavior_unchanged():
    f = FontInfo(os2_record())
    assert f.has_table() is True
    assert f.version() == 4
    assert f.weight() == 700
    assert f.width() == 5
    assert isinstance(f.weight(), int)
    assert isinstance(f.width(), int)

    missing = FontInfo(b"")
    assert missing.has_table() is False
    assert missing.version() is None
    assert missing.weight() is None

    # 确定性：两次解析结果一致
    assert FontInfo(os2_record()).weight() == FontInfo(os2_record()).weight()
    # 包导出面保持可用
    assert hasattr(os2table, "FontInfo")
