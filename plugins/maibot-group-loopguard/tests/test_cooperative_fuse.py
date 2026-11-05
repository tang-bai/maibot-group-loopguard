"""协作签名、可信名册与未知账号对话场景下的出站兜底回归。"""
from __future__ import annotations

import time
import unittest

import test_plugin_smoke as smoke
from test_plugin_smoke import make_message, plugin_module
from loopguard_core import GuardParams, GuardRegistry, StreamGuard


class OutgoingFuseCoreTest(unittest.TestCase):
    def guard(self):
        return StreamGuard(GuardParams(max_chain_length=100, outgoing_fuse_max_count=2,
                                      outgoing_fuse_window_seconds=30, outgoing_fuse_cooldown_seconds=40))

    def test_unknown_chat_cannot_reset_outgoing_budget_or_cooldown(self):
        guard = self.guard()
        for ts in (1, 3):
            guard.evaluate(now=ts, uid="unknown", is_bot=False, text=f"未知发送者第{ts}条")
            self.assertTrue(guard.check_outgoing(now=ts + 1, text=f"完全不同的消息第{ts}条", self_uid="self").allow)
        guard.evaluate(now=5, uid="other-unknown", is_bot=False, text="打断一下")
        blocked = guard.check_outgoing(now=6, text="新的回复正文", self_uid="self")
        self.assertEqual(blocked.reason, "outgoing_fuse")
        guard.evaluate(now=7, uid="unknown", is_bot=False, text="你们继续聊")
        self.assertEqual(guard.check_outgoing(now=8, text="再回复一下", self_uid="self").reason,
                         "outgoing_fuse_cooldown")
        self.assertGreater(guard.cooldown_remaining(8), 0)

    def test_only_whitelisted_human_clears_budget(self):
        guard = self.guard()
        for ts in (1, 3):
            guard.evaluate(now=ts, uid="unknown", is_bot=False, text="聊天")
            self.assertTrue(guard.check_outgoing(now=ts + 1, text=f"回复{ts}", self_uid="self").allow)
        guard.evaluate(now=5, uid="known-human", is_bot=False, text="真人发言", verified_human=True)
        self.assertEqual(guard.cooldown_remaining(5), 0)
        self.assertTrue(guard.check_outgoing(now=6, text="重新开始", self_uid="self").allow)

    def test_budget_expires_and_fuse_restores_separately(self):
        guard = self.guard()
        for ts in (1, 3):
            guard.evaluate(now=ts, uid="unknown", is_bot=False, text="聊天")
            guard.check_outgoing(now=ts + 1, text=f"回复{ts}", self_uid="self")
        self.assertTrue(guard.check_outgoing(now=50, text="过窗新回复", self_uid="self").allow)
        guard.evaluate(now=51, uid="unknown", is_bot=False, text="聊天")
        guard.check_outgoing(now=52, text="再回复", self_uid="self")
        self.assertEqual(guard.check_outgoing(now=53, text="继续回复", self_uid="self").reason, "outgoing_fuse")
        state = guard.snapshot(53)
        restored = self.guard()
        restored.restore(state, 63)
        self.assertEqual(restored.check_outgoing(now=64, text="重启后回复", self_uid="self").reason,
                         "outgoing_fuse_cooldown")
        self.assertTrue(restored.check_outgoing(now=104, text="冷却结束", self_uid="self").allow)

    def test_disabling_fuse_during_reload_drops_old_fuse_cooldown(self):
        guard = self.guard()
        for ts in (1, 3):
            guard.evaluate(now=ts, uid="unknown", is_bot=False, text="聊天")
            guard.check_outgoing(now=ts + 1, text=f"回复{ts}", self_uid="self")
        guard.check_outgoing(now=6, text="继续", self_uid="self")
        self.assertGreater(guard.cooldown_remaining(7), 0)
        state = guard.snapshot(7)
        new_params = GuardParams(max_chain_length=100, outgoing_fuse_enabled=False)
        restored = StreamGuard(new_params)
        restored.restore(state, 7)
        self.assertEqual(restored.cooldown_remaining(7), 0)
        self.assertTrue(restored.check_outgoing(now=8, text="熔断关闭后回复", self_uid="self").allow)

    def test_registry_snapshot_retains_budget_after_unknown_resets_chain(self):
        registry = GuardRegistry(GuardParams(outgoing_fuse_max_count=1, max_chain_length=100))
        guard = registry.get("room", 1, platform="qq")
        guard.check_outgoing(now=1, text="这是独立的回复正文", self_uid="self")
        guard.evaluate(now=2, uid="unknown", is_bot=False, text="你好，继续聊")
        self.assertEqual(guard.chain_length, 0)
        state = registry.snapshot(3)
        self.assertEqual(len(state), 1)
        restored = GuardRegistry(registry.params)
        restored.restore(state, 3)
        self.assertEqual(restored.get("room", 4, platform="qq").check_outgoing(
            now=4, text="另一条不同回复", self_uid="self").reason, "outgoing_fuse")

    def test_registry_streams_have_separate_budgets(self):
        registry = GuardRegistry(GuardParams(max_chain_length=100, outgoing_fuse_max_count=1))
        a = registry.get("room-a", 1, platform="qq")
        b = registry.get("room-b", 1, platform="qq")
        a.check_outgoing(now=1, text="甲", self_uid="self")
        self.assertEqual(a.check_outgoing(now=2, text="乙", self_uid="self").reason, "outgoing_fuse")
        self.assertTrue(b.check_outgoing(now=2, text="乙", self_uid="self").allow)


class CooperativeMarkerTest(unittest.TestCase):
    def test_marker_binds_account_platform_stream_body_and_age(self):
        t = 1780000000
        text = "今晚上八点线上讨论明天具体流程"
        secret = "专供受控bot共享的长密钥-测试"
        marker = plugin_module.make_bot_marker(text, secret, "qq", "group-1", "20002", t)
        verify = plugin_module.verify_bot_marker
        self.assertTrue(verify(text + " " + marker, secret, "qq", "group-1", "20002", t + 100))
        self.assertFalse(verify(text + " " + marker, secret, "qq", "group-1", "other", t))
        self.assertFalse(verify(text + " " + marker, secret, "qq", "group-2", "20002", t))
        self.assertFalse(verify(text + " " + marker, secret, "tg", "group-1", "20002", t))
        self.assertFalse(verify("改动了文本 " + marker, secret, "qq", "group-1", "20002", t))
        self.assertFalse(verify(text + "！ " + marker, secret, "qq", "group-1", "20002", t))
        self.assertFalse(verify(text + " " + marker, secret, "qq", "group-1", "20002", t + 301))
        self.assertFalse(verify(text + " " + marker, "wrong", "qq", "group-1", "20002", t))
        self.assertFalse(verify(text + " [BOT:20002]", secret, "qq", "group-1", "20002", t))
        self.assertFalse(verify(text + " " + marker + " 其他文本", secret, "qq", "group-1", "20002", t))

    def test_missing_secret_or_sender_cannot_sign(self):
        with self.assertRaises(ValueError):
            plugin_module.make_bot_marker("hi", "", "qq", "room", "42", time.time())
        with self.assertRaises(ValueError):
            plugin_module.make_bot_marker("hi", "secret", "qq", "room", "self", time.time())


class FusePluginTest(unittest.IsolatedAsyncioTestCase):
    """用相同Hook替身配置覆盖协作协议和出站预算。"""

    async def asyncSetUp(self):
        await smoke.PluginHookTest.asyncSetUp(self)

    async def asyncTearDown(self):
        await smoke.PluginHookTest.asyncTearDown(self)

    async def test_fuse_unknown_accounts_cannot_reset(self):
        self.plugin.config.action.outgoing_fuse_max_count = 2
        self.plugin.config.detect.max_chain_length = 100
        self.plugin._registry = GuardRegistry(self.plugin._build_params())
        for i in range(2):
            self.assertIsNone(await self.plugin.guard_incoming(message=make_message(f"human{i}", "你觉得呢")))
            self.assertIsNone(await self.plugin.guard_outgoing(message=make_message("10001", f"第{i}条新的回复内容")))
        self.assertIsNone(await self.plugin.guard_incoming(message=make_message("unknown", "另一条未知人发言")))
        self.assertEqual(await self.plugin.guard_outgoing(message=make_message("10001", "第三条新回复内容")),
                         {"action": "abort"})
        guard = self.plugin._registry.get("stream-1", time.time(), platform="qq")
        self.assertGreater(guard.cooldown_remaining(time.time()), 0)

    async def test_whitelisted_human_clears_outgoing_budget(self):
        self.plugin.config.identity.human_accounts = ["qq:member"]
        self.plugin.config.action.outgoing_fuse_max_count = 1
        self.plugin.config.detect.max_chain_length = 100
        self.plugin._registry = GuardRegistry(self.plugin._build_params())
        self.assertIsNone(await self.plugin.guard_outgoing(message=make_message("10001", "第一条全新内容")))
        self.assertIsNone(await self.plugin.guard_incoming(message=make_message("member", "真人介入")))
        self.assertIsNone(await self.plugin.guard_outgoing(message=make_message("10001", "第二条全新内容")))

    async def test_protocol_bot_flag_is_opt_in_and_never_overrides_human_whitelist(self):
        message = make_message("40004", "这是一条真实聊天消息")
        message["sender"] = {"bot": True}
        self.assertIsNone(await self.plugin.guard_incoming(message=message))
        guard = self.plugin._registry.get("stream-1", time.time(), platform="qq")
        self.assertFalse(guard.senders(time.time())[0]["is_bot"])
        self.plugin.config.identity.trust_protocol_bot_flag = True
        self.assertIsNone(await self.plugin.guard_incoming(message=message))
        self.assertTrue(guard.senders(time.time())[0]["is_bot"])
        self.plugin.config.identity.human_accounts = ["40004"]
        self.assertIsNone(await self.plugin.guard_incoming(message=message))
        self.assertFalse(guard.senders(time.time())[0]["is_bot"])

    async def test_hmac_signed_incoming_and_outgoing_round_trip(self):
        self.plugin.config.identity.cooperative_secret = "shared-test-key"
        self.plugin.config.identity.sign_outgoing = True
        outbound = make_message("10001", "我要告诉大家明天晚上活动的完整安排")
        result = await self.plugin.guard_outgoing(message=outbound)
        self.assertEqual(result["action"], "continue")
        signed = result["modified_kwargs"]["message"]
        self.assertNotEqual(outbound["raw_message"], signed["raw_message"])
        text = plugin_module._extract_text(signed)
        self.assertTrue(plugin_module.verify_bot_marker(text, "shared-test-key", "qq", "group:123456", "10001", time.time()))
        incoming = make_message("40004", "一条完全不同的外来机器人消息")
        marker = plugin_module.make_bot_marker(plugin_module._extract_text(incoming), "shared-test-key",
                                               "qq", "group:123456", "40004", time.time())
        incoming["raw_message"].append({"type": "text", "data": " " + marker})
        self.assertIsNone(await self.plugin.guard_incoming(message=incoming))
        guard = self.plugin._registry.get("stream-1", time.time(), platform="qq")
        entry = next(e for e in guard.senders(time.time()) if e["uid"] == "40004")
        self.assertTrue(entry["is_bot"])
        self.assertEqual(entry["identity_source"], "协作签名已验证")
        incoming["session_id"] = "other-bot-session-identifier"
        self.assertTrue(plugin_module.verify_bot_marker(plugin_module._extract_text(incoming), "shared-test-key",
                                                        "qq", plugin_module._signature_scope(incoming, "other-bot-session-identifier"),
                                                        "40004", time.time()))
        incoming["message_info"]["group_info"]["group_id"] = "other-group"
        self.assertFalse(plugin_module.verify_bot_marker(plugin_module._extract_text(incoming), "shared-test-key",
                                                         "qq", plugin_module._signature_scope(incoming, "other-bot-session-identifier"),
                                                         "40004", time.time()))

    async def test_status_and_who_explain_fuse_and_verified_marker(self):
        self.plugin.config.action.outgoing_fuse_max_count = 1
        self.plugin._registry = GuardRegistry(self.plugin._build_params())
        self.plugin.config.identity.cooperative_secret = "shared-key"
        text = "对跨群机器人互认做一个测试消息"
        signed = make_message("40004", text)
        marker = plugin_module.make_bot_marker(text, "shared-key", "qq", "group:123456", "40004", time.time())
        signed["raw_message"].append({"type": "text", "data": " " + marker})
        await self.plugin.guard_incoming(message=signed)
        who = await self.plugin.handle_command(is_local_operator=True, stream_id="stream-1", matched_groups={"sub": "who"})
        self.assertIn("协作签名已验证", who[1])
        await self.plugin.guard_outgoing(message=make_message("10001", "这是全新的回复"))
        await self.plugin.guard_outgoing(message=make_message("10001", "第二条全新回复"))
        result = await self.plugin.handle_command(is_local_operator=True, stream_id="stream-1")
        self.assertIn("出站窗口 1/1", result[1])
        self.assertIn("独立出站熔断", result[1])

    async def test_peer_roster_refresh_and_reload_preserves_fuse(self):
        self.plugin.config.identity.peer_bot_config_key = "bot.peer_bot_ids"
        self.config_cap.values["bot.peer_bot_ids"] = ["qq:40004"]
        await self.plugin._resolve_peer_roster()
        self.assertTrue(self.plugin._is_bot_account("qq", "40004", "人名", "", time.time()))
        self.plugin.config.action.outgoing_fuse_max_count = 1
        await self.plugin.on_config_update("plugin", {}, "1")
        self.assertIsNone(await self.plugin.guard_outgoing(message=make_message("10001", "首次全新回复")))
        await self.plugin.on_config_update("plugin", {}, "2")
        # 重载后短窗内计数也保留；第二次发送立即熔断。
        self.assertEqual(await self.plugin.guard_outgoing(message=make_message("10001", "再次全新回复")), {"action": "abort"})
        await self.plugin.on_config_update("plugin", {}, "3")
        self.assertGreater(self.plugin._registry.get("stream-1", time.time(), platform="qq").cooldown_remaining(time.time()), 0)


if __name__ == "__main__":
    unittest.main()
