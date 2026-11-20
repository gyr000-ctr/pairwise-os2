"""os2table 核心 API（初始未实现）。"""


class FontInfo:
    def __init__(self, data):
        """解析 OS/2 表"""
        raise NotImplementedError

    def version(self):
        """版本"""
        raise NotImplementedError

    def weight(self):
        """字重"""
        raise NotImplementedError

    def width(self):
        """字宽"""
        raise NotImplementedError

    def has_table(self):
        """是否存在"""
        raise NotImplementedError
