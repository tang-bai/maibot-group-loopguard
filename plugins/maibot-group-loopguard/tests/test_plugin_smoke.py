"""`plugin.py` 的 Hook 级冒烟测试。

用 `tests/stubs/maibot_sdk` 顶替真实 SDK，在没有 MaiBot 环境的情况下把
`on_load` / 入站 Hook / 出站 Hook / 命令处理都跑一遍。

运行：

    cd plugins/maibot-group-loopguard
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent / "stubs"))
sys.path.insert(1, str(_PLUGIN_DIR))

_SPEC = importlib.util.spec_from_file_location(
    "loopguard_plugin_under_test",
    str(_PLUGIN_DIR / "plugin.py"),
    submodule_search_locations=[str(_PLUGIN_DIR)],
)
assert _SPEC is not None and _SPEC.loader is not None
plugin_module = importlib.util.module_from_spec(_SPEC)
sys.modules["loopguard_plugin_under_test"] = plugin_module
_SPEC.loader.exec_module(plugin_module)


class FakeConfigCapability:
    """替身：模拟 `ctx.config.get()` 读全局 Bot 配置。"""

    def __init__(self) -> None:
        self.values = {"bot.platform": "qq", "bot.qq_account": "10001", "bot.platforms": ["qq:10001"]}

    async def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)


class FakeSendCapability:
    """替身：记录插件发出的提示消息。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def text(self, text: str, stream_id: str, **kwargs: object) -> bool:
        self.sent.append((stream_id, text))
        return True


class FakeFrequencyCapability:
    """替身：记录频率调整。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    async def set_adjust(self, chat_id: str, value: float) -> bool:
        self.calls.append((chat_id, value))
        return True


def make_message(
    uid: str,
    text: str,
    *,
    platform: str = "qq",
    session: str = "stream-1",
    nickname: str = "群友",
    at: str = "",
) -> dict[str, object]:
    """构造一条 `chat.receive.before_process` 载荷。"""

    raw: list[dict[str, object]] = [{"type": "text", "data": text}]
    if at:
        raw.append({"type": "at", "data": {"target_user_id": at, "target_user_nickname": "被@"}})
    return {
        "message_id": f"msg-{uid}-{abs(hash(text)) % 100000}",
        "platform": platform,
        "session_id": session,
        "message_info": {
            "user_info": {"user_id": uid, "user_nickname": nickname, "user_cardname": ""},
            "group_info": {"group_id": "123456", "group_name": "测试群"},
            "additional_config": {},
        },
        "raw_message": raw,
    }


class PluginHookTest(unittest.IsolatedAsyncioTestCase):
    """覆盖入站、出站、命令三条路径。"""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plugin = plugin_module.create_plugin()
        self.config_cap = FakeConfigCapability()
        self.send_cap = FakeSendCapability()
        self.freq_cap = FakeFrequencyCapability()
        self.ctx = SimpleNamespace(
            logger=logging.getLogger("test.loopguard"),
            paths=SimpleNamespace(data_dir=Path(self._tmp.name)),
            config=self.config_cap,
            send=self.send_cap,
            frequency=self.freq_cap,
        )
        self.plugin._set_context(self.ctx)
        # 默认配置：把另一只机器人登记进名单
        self.plugin.config.identity.bot_accounts = ["20002"]
        await self.plugin.on_load()

    async def asyncTearDown(self) -> None:
        await self.plugin.on_unload()

    async def test_duplicate_from_other_bot_is_aborted(self) -> None:
        first = await self.plugin.guard_incoming(
            message=make_message("20002", "今晚一起打游戏吗我这边随时可以")
        )
        self.assertIsNone(first)

        second = await self.plugin.guard_incoming(
            message=make_message("20002", "今晚一起打游戏吗？我这边随时都可以！")
        )
        self.assertEqual(second, {"action": "abort"})

    async def test_human_message_resets_chain(self) -> None:
        await self.plugin.guard_incoming(message=make_message("20002", "今晚一起打游戏吗我这边随时可以"))
        await self.plugin.guard_incoming(message=make_message("20002", "今晚一起打游戏吗？我这边随时都可以！"))

        human = await self.plugin.guard_incoming(message=make_message("10086", "我八点到"))
        self.assertIsNone(human)

        again = await self.plugin.guard_incoming(message=make_message("20002", "今晚一起打游戏吗我这边随时可以"))
        self.assertIsNone(again)

    async def test_self_echo_is_aborted(self) -> None:
        decision = await self.plugin.guard_incoming(message=make_message("10001", "这是我自己发出去的消息"))
        self.assertEqual(decision, {"action": "abort"})

    async def test_outgoing_duplicate_is_aborted(self) -> None:
        await self.plugin.guard_incoming(message=make_message("20002", "群里的活动安排已经发出来了大家记得看"))
        decision = await self.plugin.guard_outgoing(
            message={
                "platform": "qq",
                "session_id": "stream-1",
                "message_info": {"user_info": {"user_id": "10001"}},
                "raw_message": [{"type": "text", "data": "群里的活动安排已经发出来了，大家记得看一下"}],
            }
        )
        self.assertEqual(decision, {"action": "abort"})

    async def test_dry_run_lets_everything_through(self) -> None:
        self.plugin.config.plugin.dry_run = True
        await self.plugin.guard_incoming(message=make_message("20002", "今晚一起打游戏吗我这边随时可以"))
        decision = await self.plugin.guard_incoming(
            message=make_message("20002", "今晚一起打游戏吗？我这边随时都可以！")
        )
        self.assertIsNone(decision)

    async def test_platform_scope_skips_other_platforms(self) -> None:
        decision = await self.plugin.guard_incoming(
            message=make_message("20002", "今晚一起打游戏吗我这边随时可以", platform="telegram", session="tg-1")
        )
        self.assertIsNone(decision)

    async def test_block_incoming_disabled_keeps_outgoing_guard(self) -> None:
        self.plugin.config.action.block_incoming = False
        await self.plugin.guard_incoming(message=make_message("20002", "今晚一起打游戏吗我这边随时可以"))
        incoming = await self.plugin.guard_incoming(
            message=make_message("20002", "今晚一起打游戏吗？我这边随时都可以！")
        )
        self.assertIsNone(incoming)  # 入站放行

        outgoing = await self.plugin.guard_outgoing(
            message={
                "platform": "qq",
                "session_id": "stream-1",
                "message_info": {"user_info": {"user_id": "10001"}},
                "raw_message": [{"type": "text", "data": "一句全新的回复内容"}],
            }
        )
        self.assertEqual(outgoing, {"action": "abort"})  # 冷却已建立，出站仍然拦

    async def test_command_status_reports_state(self) -> None:
        await self.plugin.guard_incoming(message=make_message("20002", "今晚一起打游戏吗我这边随时可以"))
        await self.plugin.guard_incoming(message=make_message("20002", "今晚一起打游戏吗？我这边随时都可以！"))
        success, text, intercept = await self.plugin.handle_command(
            is_local_operator=True,
            user_id="10001",
            stream_id="stream-1",
            matched_groups={"sub": "status", "arg": None},
        )
        self.assertTrue(success)
        self.assertTrue(intercept)
        self.assertIn("剩余冷却", text)

    async def test_behavior_learning_marks_unknown_sender(self) -> None:
        await self.plugin.guard_incoming(message=make_message("30003", "今天下午三点在会议室开个短会", nickname="路人甲"))
        first_echo = await self.plugin.guard_incoming(
            message=make_message("40004", "今天下午三点在会议室开个短会。", nickname="路人乙")
        )
        # 首次复述只登记身份、不额外惩罚（避免误伤真人引用）
        self.assertIsNone(first_echo)
        self.assertIn("40004", self.plugin._learned)

        # 上名单之后，同一账号再复述就按机器人规则拦下
        second_echo = await self.plugin.guard_incoming(
            message=make_message("40004", "今天下午三点在会议室开个短会，记得带材料", nickname="路人乙")
        )
        self.assertEqual(second_echo, {"action": "abort"})


class NoLoopTest(unittest.IsolatedAsyncioTestCase):
    """确认后台落盘任务不会阻塞事件循环退出。"""

    async def test_state_written_on_unload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = plugin_module.create_plugin()
            plugin.config.identity.bot_accounts = ["20002"]
            plugin._set_context(
                SimpleNamespace(
                    logger=logging.getLogger("test.loopguard.state"),
                    paths=SimpleNamespace(data_dir=Path(tmp)),
                    config=FakeConfigCapability(),
                    send=FakeSendCapability(),
                    frequency=FakeFrequencyCapability(),
                )
            )
            await plugin.on_load()
            await plugin.guard_incoming(message=make_message("20002", "一条会写进状态的机器人消息"))
            await plugin.on_unload()
            state_file = Path(tmp) / "loopguard_state.json"
            self.assertTrue(state_file.exists())
            content = state_file.read_text(encoding="utf-8")
            self.assertIn("stream-1", content)
            await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
