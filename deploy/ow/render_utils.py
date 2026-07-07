"""通用图片渲染工具

目前提供 save_temp_image，用于将生成的图片 bytes 保存到插件数据目录。
AstrBot 文转图推荐直接使用 Star.html_render()，渲染部分不在此模块。
"""

import time
from pathlib import Path


def save_temp_image(img_bytes: bytes, dir_path: str, prefix: str = "render") -> str:
    """将图片 bytes 保存到指定目录，返回文件路径。

    Args:
        img_bytes: PNG 图片二进制数据。
        dir_path: 目标目录路径（应符合 AstrBot 规范：data/plugin_data/{plugin_name}/ 下）。
        prefix: 文件名前缀。

    Returns:
        落地后的文件绝对路径，可直接传入 event.image_result()。
    """
    target = Path(dir_path)
    target.mkdir(parents=True, exist_ok=True)
    f = target / f"{prefix}_{int(time.time() * 1000)}.png"
    f.write_bytes(img_bytes)
    return str(f)
