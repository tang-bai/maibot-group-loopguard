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
import json
import sys
import tempfile
import unittest
import unittest.mock
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
        # 显式注入配置实例：无论用真实 SDK 还是替身，都能直接改字段
        self.plugin._plugin_config_instance = plugin_module.LoopGuardConfig()
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

    async def test_who_command_lists_verdicts(self) -> None:
        await self.plugin.guard_incoming(message=make_message("20002", "机器人说了一句"))
        await self.plugin.guard_incoming(message=make_message("10086", "人类插嘴"))
        success, text, _ = await self.plugin.handle_command(
            is_local_operator=True,
            user_id="10001",
            stream_id="stream-1",
            matched_groups={"sub": "who", "arg": None},
        )
        self.assertTrue(success)
        self.assertIn("20002", text)
        self.assertIn("机器人", text)
        self.assertIn("10086", text)

    async def test_human_at_known_bot_stays_unknown(self) -> None:
        await self.plugin.guard_incoming(message=make_message("20002", "机器人甲说一句话"))
        for _ in range(5):
            result = await self.plugin.guard_incoming(message=make_message("99999", "那你觉得呢", at="10001"))
            self.assertIsNone(result)
        self.assertEqual(self.plugin._learned, {})
        guard = self.plugin._registry.get("stream-1", plugin_module.time.time())
        self.assertFalse(next(item for item in guard.senders(plugin_module.time.time()) if item["uid"] == "99999")["is_bot"])

    async def test_frequency_suspect_marks_bot_after_three_bursts(self) -> None:
        self.plugin.config.suspect.enabled = True
        self.plugin.config.suspect.min_messages = 3
        self.plugin.config.suspect.strikes_to_mark = 3
        self.plugin._registry = plugin_module.GuardRegistry(self.plugin._build_params())

        async def run_burst(base: float) -> None:
            for turn in range(3):
                ts = base + turn * 2
                with unittest.mock.patch.object(plugin_module.time, "time", return_value=ts):
                    await self.plugin.guard_incoming(message=make_message("20002", f"别人第 {turn} 句"))
                with unittest.mock.patch.object(plugin_module.time, "time", return_value=ts + 1):
                    await self.plugin.guard_incoming(message=make_message("40004", f"接话第 {turn} 句"))

        for window in range(3):
            await run_burst(float(window * 90))

        self.assertTrue(self.plugin._learned_hit("40004", 190, platform="qq", stream_id="stream-1"))
        self.assertFalse(self.plugin._learned_hit("30003", 190, platform="qq", stream_id="stream-1"))

    async def test_frequency_suspect_disabled_by_default(self) -> None:
        self.assertFalse(self.plugin.config.suspect.enabled)
        for turn in range(6):
            await self.plugin.guard_incoming(message=make_message("30003", f"别人第 {turn} 句"))
            await self.plugin.guard_incoming(message=make_message("40004", f"接话第 {turn} 句"))
        self.assertEqual(self.plugin._learned, {}, "默认关闭时不应产生任何标记")

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

    async def test_behavior_learning_requires_multiple_distinct_echoes(self) -> None:
        samples = ["今天下午三点在会议室开个短会记得带材料", "周末准备去海边看看日落顺便拍一组照片", "服务器版本已经更新请各位重新启动客户端"]
        for index, text in enumerate(samples):
            await self.plugin.guard_incoming(message=make_message("20002", text))
            result = await self.plugin.guard_incoming(message=make_message("40004", text + "。"))
            self.assertIsNone(result, "累计证据本条仍放行")
            learned = self.plugin._learned_hit("40004", plugin_module.time.time(), platform="qq", stream_id="stream-1")
            self.assertEqual(learned, index == 2)
        for text in ["先谈一点全新的活动组织方案", "然后再讨论预算和报名时间", "最后确认场地和交通路线安排", "另外还有餐饮和应急联络事宜"]:
            result = await self.plugin.guard_incoming(message=make_message("40004", text))
        self.assertEqual(result, {"action": "abort"})

    async def test_single_human_copy_is_not_learned_or_blocked(self):
        text = "今天下午三点在会议室开个短会记得带材料"
        await self.plugin.guard_incoming(message=make_message("20002", text))
        self.assertIsNone(await self.plugin.guard_incoming(message=make_message("40004", text)))
        self.assertEqual(self.plugin._learned, {})

    async def test_reply_quote_does_not_count_as_authored_text(self):
        text = "今天下午三点在会议室开个短会记得带材料"
        message = make_message("40004", "收到")
        message["raw_message"].insert(0, {"type": "reply", "data": {"target_message_content": text}})
        self.assertEqual(plugin_module._extract_text(message).strip(), "收到")
        for _ in range(5):
            await self.plugin.guard_incoming(message=make_message("20002", text))
            self.assertIsNone(await self.plugin.guard_incoming(message=message))
        self.assertEqual(self.plugin._learned, {})

    async def test_at_component_keeps_adjacent_chinese_body(self):
        text = "今天下午三点在会议室开个短会记得带材料"
        message = make_message("40004", text, at="10001")
        message["raw_message"].reverse()
        self.assertEqual(plugin_module.normalize_text(plugin_module._extract_text(message)), text)
        message["raw_message"] = [{"type": "text", "data": {"text": text}}]
        self.assertEqual(plugin_module._extract_text(message), text)

    async def test_human_whitelist_overrides_all_nonself_signals(self):
        self.plugin.config.identity.human_accounts = ["qq:40004"]
        self.plugin.config.identity.bot_accounts.append("40004")
        self.plugin.config.identity.trust_name_patterns = True
        self.plugin.config.identity.name_patterns = [".*"]
        self.plugin.config.suspect.enabled = True
        for index in range(6):
            text = f"今天下午三点在会议室开会讨论第{index}份不同材料"
            await self.plugin.guard_incoming(message=make_message("20002", text))
            self.assertIsNone(await self.plugin.guard_incoming(message=make_message("40004", text, at="10001")))
        self.assertEqual(self.plugin._learned, {})
        self.assertFalse(self.plugin._is_bot_account("qq", "40004", "SomeBot", "", plugin_module.time.time(), "stream-1"))

    async def test_nickname_is_weak_unless_explicitly_trusted(self):
        now = plugin_module.time.time()
        self.assertFalse(self.plugin._is_bot_account("qq", "40004", "HelpBot", "真人群名片", now))
        self.plugin.config.identity.trust_name_patterns = True
        self.assertTrue(self.plugin._is_bot_account("qq", "40004", "HelpBot", "真人群名片", now))
        self.plugin.config.identity.name_patterns = []
        self.assertFalse(self.plugin._is_bot_account("qq", "40004", "HelpBot", "", now))

    async def test_auto_learning_is_scoped_by_platform_and_stream(self):
        now = plugin_module.time.time()
        self.plugin._remember_learned("40004", now, platform="qq", stream_id="stream-1", source="test")
        self.assertTrue(self.plugin._learned_hit("40004", now, platform="qq", stream_id="stream-1"))
        self.assertFalse(self.plugin._learned_hit("40004", now, platform="qq", stream_id="stream-2"))
        self.assertFalse(self.plugin._learned_hit("40004", now, platform="telegram", stream_id="stream-1"))
        self.assertFalse(self.plugin._learned_hit("40004", now + 4000, platform="qq", stream_id="stream-1"))

    async def test_platform_specific_bot_list_does_not_match_other_platform(self):
        self.plugin.config.identity.bot_accounts = ["telegram:40004"]
        now = plugin_module.time.time()
        self.assertFalse(self.plugin._is_bot_account("qq", "40004", "群友", "", now))
        self.assertTrue(self.plugin._is_bot_account("telegram", "40004", "群友", "", now))
        for _ in range(5):
            self.assertIsNone(await self.plugin.guard_incoming(message=make_message("50005", "你好呀", at="40004")))

    async def test_who_reports_current_identity_and_evidence(self):
        text = "今天下午三点在会议室开个短会记得带材料"
        await self.plugin.guard_incoming(message=make_message("20002", text))
        await self.plugin.guard_incoming(message=make_message("40004", text))
        result = await self.plugin.handle_command(is_local_operator=True, stream_id="stream-1", matched_groups={"sub": "who"})
        self.assertIn("待观察/身份未知", result[1])
        self.assertIn("回声1次", result[1])
        self.plugin._remember_learned("40004", plugin_module.time.time(), platform="qq", stream_id="stream-1", source="test")
        result = await self.plugin.handle_command(is_local_operator=True, stream_id="stream-1", matched_groups={"sub": "who"})
        self.assertIn("临时学习：test", result[1])

    async def test_missing_session_uses_same_stable_group_guard(self):
        text = "今晚一起打游戏吗我这边随时可以"
        message = make_message("20002", text)
        message.pop("session_id")
        self.assertIsNone(await self.plugin.guard_incoming(message=message))
        message["message_id"] = "different-id"
        self.assertEqual(await self.plugin.guard_incoming(message=message), {"action": "abort"})
        outgoing = make_message("10001", "这是一条全新的出站消息")
        outgoing.pop("session_id")
        self.assertEqual(await self.plugin.guard_outgoing(message=outgoing), {"action": "abort"})

    async def test_missing_sender_does_not_clear_cooldown(self):
        text = "今晚一起打游戏吗我这边随时可以"
        await self.plugin.guard_incoming(message=make_message("20002", text))
        await self.plugin.guard_incoming(message=make_message("20002", text))
        now = plugin_module.time.time()
        guard = self.plugin._registry.get("stream-1", now)
        await self.plugin.guard_incoming(message=make_message("", "系统通知"))
        self.assertGreater(guard.cooldown_remaining(now), 0)

    async def test_dry_run_never_modifies_planner_tools(self):
        self.plugin.config.action.soft_mute_reply_tool = True
        self.plugin.config.plugin.dry_run = True
        text = "今晚一起打游戏吗我这边随时可以"
        await self.plugin.guard_incoming(message=make_message("20002", text))
        await self.plugin.guard_incoming(message=make_message("20002", text))
        tools = [{"function": {"name": "reply"}}]
        result = await self.plugin.soft_mute_planner(session_id="stream-1", tool_definitions=tools)
        self.assertIsNone(result)
        self.assertEqual(len(tools), 1)

    async def test_manual_learn_and_forget_respect_platform_prefix(self):
        result = await self.plugin.handle_command(is_local_operator=True, stream_id="stream-1", matched_groups={"sub": "learn", "arg": "qq:40004"})
        self.assertTrue(result[0])
        now = plugin_module.time.time()
        self.assertTrue(self.plugin._learned_hit("40004", now, platform="qq", stream_id="stream-1"))
        self.assertFalse(self.plugin._learned_hit("40004", now, platform="telegram", stream_id="stream-1"))
        await self.plugin.handle_command(is_local_operator=True, matched_groups={"sub": "forget", "arg": "qq:40004"})
        self.assertFalse(self.plugin._learned_hit("40004", now, platform="qq", stream_id="stream-1"))

    async def test_old_state_drops_unscoped_single_signal_learning(self):
        self.plugin._state_path.write_text(json.dumps({"version": 1, "learned": {"40004": plugin_module.time.time() + 9999}}), encoding="utf-8")
        self.plugin._load_state()
        self.assertEqual(self.plugin._learned, {})

    async def test_mixed_platform_sessions_never_share_identity_evidence(self):
        self.plugin.config.identity.platform_scope = []
        self.plugin.config.identity.bot_accounts = ["qq:20002"]
        samples = ["今天下午三点在会议室开个短会记得带材料", "周末准备去海边看看日落顺便拍一组照片", "服务器版本已经更新请各位重新启动客户端"]
        for index, text in enumerate(samples):
            await self.plugin.guard_incoming(message=make_message("20002", text, session="same"))
            platform = "qq" if index < 2 else "telegram"
            await self.plugin.guard_incoming(message=make_message("40004", text, platform=platform, session="same"))
        self.assertEqual(self.plugin._learned, {})
        now = plugin_module.time.time()
        qq = self.plugin._registry.get("same", now, platform="qq")
        tg = self.plugin._registry.get("same", now, platform="telegram")
        self.assertEqual(qq.identity_report(now=now, uid="40004").echoes, 2)
        self.assertEqual(tg.identity_report(now=now, uid="40004").echoes, 0)

    async def test_expired_learning_needs_fresh_unknown_evidence(self):
        self.plugin.config.identity.learned_ttl_seconds = 60
        samples = ["今天下午三点在会议室开个短会记得带材料", "周末准备去海边看看日落顺便拍一组照片", "服务器版本已经更新请各位重新启动客户端"]
        for index, text in enumerate(samples):
            with unittest.mock.patch.object(plugin_module.time, "time", return_value=index * 2):
                await self.plugin.guard_incoming(message=make_message("20002", text))
            with unittest.mock.patch.object(plugin_module.time, "time", return_value=index * 2 + 1):
                await self.plugin.guard_incoming(message=make_message("40004", text))
        self.assertTrue(self.plugin._learned_hit("40004", 6, platform="qq", stream_id="stream-1"))
        with unittest.mock.patch.object(plugin_module.time, "time", return_value=66):
            await self.plugin.guard_incoming(message=make_message("40004", "我刚才去买了一杯咖啡现在已经回来了"))
        self.assertFalse(self.plugin._learned_hit("40004", 66, platform="qq", stream_id="stream-1"))

    async def test_who_can_select_platform_when_session_names_collide(self):
        self.plugin.config.identity.platform_scope = []
        for platform in ("qq", "telegram"):
            await self.plugin.guard_incoming(message=make_message("40004", "普通消息", platform=platform, session="same"))
        self.plugin._remember_learned("40004", plugin_module.time.time(), platform="qq", stream_id="same", source="test")
        result = await self.plugin.handle_command(is_local_operator=True, matched_groups={"sub": "who", "arg": "same"})
        self.assertFalse(result[0])
        result = await self.plugin.handle_command(is_local_operator=True, matched_groups={"sub": "who", "arg": "qq:same"})
        self.assertTrue(result[0])
        self.assertIn("临时学习：test", result[1])

    async def test_restart_subtracts_elapsed_cooldown_and_preserves_scope(self):
        now = plugin_module.time.time()
        self.plugin._remember_learned("40004", now, platform="qq", stream_id="stream-1", source="test")
        text = "今晚一起打游戏吗我这边随时可以"
        await self.plugin.guard_incoming(message=make_message("20002", text))
        await self.plugin.guard_incoming(message=make_message("20002", text))
        await self.plugin._flush_state()
        self.plugin._learned.clear()
        self.plugin._learned_sources.clear()
        self.plugin._registry = plugin_module.GuardRegistry(self.plugin._build_params())
        with unittest.mock.patch.object(plugin_module.time, "time", return_value=now + 1000):
            self.plugin._load_state()
        self.assertTrue(self.plugin._learned_hit("40004", now + 1000, platform="qq", stream_id="stream-1"))
        self.assertFalse(self.plugin._learned_hit("40004", now + 1000, platform="telegram", stream_id="stream-1"))
        self.assertEqual(self.plugin._registry.get("stream-1", now + 1000).cooldown_remaining(now + 1000), 0)


class NoLoopTest(unittest.IsolatedAsyncioTestCase):
    """确认后台落盘任务不会阻塞事件循环退出。"""

    async def test_state_written_on_unload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = plugin_module.create_plugin()
            plugin._plugin_config_instance = plugin_module.LoopGuardConfig()
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
