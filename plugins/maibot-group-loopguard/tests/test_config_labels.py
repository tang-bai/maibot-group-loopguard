"""配置 Schema 的中文字段名回归测试。

需要真实 `maibot-plugin-sdk`（会 import pydantic）。在没有安装 SDK 的环境里
自动跳过，保证 `python -m unittest discover` 依旧可跑。

这个测试守的是一条容易回归的约定：WebUI 的字段标题取自 Schema 的 `label`，
缺省会退回字段名（`window_seconds` 这种蛇形英文）。所有字段都必须显式给中文名。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[1]

try:
    import maibot_sdk

    _HAS_REAL_SDK = hasattr(maibot_sdk, "__version__")
except Exception:  # pragma: no cover - 未安装 SDK 的环境
    _HAS_REAL_SDK = False


def _has_cjk(text: str) -> bool:
    """判断文本里是否含有 CJK 字符。"""

    return any("\u4e00" <= char <= "\u9fff" for char in text)


@unittest.skipUnless(_HAS_REAL_SDK, "需要真实 maibot-plugin-sdk（含 pydantic）才能生成配置 Schema")
class ConfigLabelTest(unittest.TestCase):
    """检查每个配置字段都带了中文显示名。"""

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location(
            "loopguard_plugin_schema",
            str(_PLUGIN_DIR / "plugin.py"),
            submodule_search_locations=[str(_PLUGIN_DIR)],
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["loopguard_plugin_schema"] = module
        spec.loader.exec_module(module)
        cls.module = module
        cls.schema = module.LoopGuardPlugin.build_config_schema(plugin_id="maibot-community.group-loop-guard")

    def test_every_field_has_chinese_label(self) -> None:
        unlocalized: list[str] = []
        for section in self.schema["sections"].values():
            for name, field in section["fields"].items():
                label = str(field.get("label") or "")
                if label == name or not _has_cjk(label):
                    unlocalized.append(f"{section['name']}.{name}")
        self.assertEqual(unlocalized, [], f"这些字段缺少中文显示名：{unlocalized}")

    def test_section_titles_are_chinese(self) -> None:
        for name, section in self.schema["sections"].items():
            self.assertTrue(_has_cjk(str(section.get("title") or "")), f"配置节 {name} 标题缺少中文")

    def test_field_count_matches_config_model(self) -> None:
        total = sum(len(section["fields"]) for section in self.schema["sections"].values())
        self.assertEqual(total, 47, f"字段数量变化了（当前 {total}），记得同步文档里的配置全表")


if __name__ == "__main__":
    unittest.main()
