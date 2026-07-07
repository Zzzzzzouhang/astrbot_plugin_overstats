"""指令 mixin 模块子包。

各 plugin_*.py 为 OverstatsPlugin 的 mixin，运行时由 deploy/../main.py
通过多继承聚合。内部相对导入使用 ``from ..deploy`` 指向同级 deploy 包。
"""