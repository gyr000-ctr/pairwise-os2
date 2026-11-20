import math
import struct

import pytest

import os2table

def _raises(fn, exc=ValueError):
    try:
        fn()
    except exc:
        return True
    return False

import struct

BASE = struct.pack(">H", 4) + struct.pack(">h", 700) + struct.pack(">H", 5) + b"\x00" * 80


def _f(data=None):
    return os2table.FontInfo(BASE if data is None else data)

CASES = [
        ("version", lambda: _f().version() == 4),
        ("weight", lambda: _f().weight() == 700),
        ("width", lambda: _f().width() == 5),
        ("has", lambda: _f().has_table() is True),
        ("missing", lambda: os2table.FontInfo(b"").has_table() is False),
        ("missing_version", lambda: os2table.FontInfo(b"").version() is None),
        ("truncated", lambda: _raises(lambda: _f(b"\x00\x04"))),
        ("bad_version", lambda: _raises(lambda: _f(struct.pack(">H", 99) + BASE[2:]))),
        ("type", lambda: isinstance(_f().weight(), int)),
        ("width_type", lambda: isinstance(_f().width(), int)),
        ("deterministic", lambda: _f().weight() == _f().weight()),
        ("weight_range", lambda: 0 <= _f().weight() <= 1000),
        ("width_range", lambda: 1 <= _f().width() <= 9),
        ("version_zero", lambda: _f(struct.pack(">H", 0) + BASE[2:]).version() == 0),
        ("no_mutation", lambda: (lambda b: (_f(b), b == BASE)[1])(BASE)),
    ]


@pytest.mark.parametrize("name,check", CASES, ids=[c[0] for c in CASES])
def test_spec(name, check):
    assert check() is True
