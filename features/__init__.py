"""Overstats 指令业务逻辑拆分模块。

本包内的函数承载 main.py 中各个 @filter.command 指令处理器（handler）的具体实现。
main.py 的 OverstatsPlugin 类仅保留指令/事件注册（@filter.* 装饰器）与必要的辅助方法，
handler 方法作为薄封装委托到本包对应的函数，函数签名统一为
``func(plugin, event, *args)``，其中 plugin 即 OverstatsPlugin 实例，
通过 plugin.xxx 访问其辅助方法与实例状态（以保持与原有 self.xxx 调用一致）。
"""
