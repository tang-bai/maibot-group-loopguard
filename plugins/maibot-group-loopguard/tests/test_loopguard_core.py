"""`loopguard_core` 的纯标准库回归测试。

运行方式（无需安装 MaiBot，也无需 pytest）：

    cd plugins/maibot-group-loopguard
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loopguard_core import (  # noqa: E402
    GuardParams,
    GuardRegistry,
    StreamGuard,
    bigram_containment,
    bigram_similarity,
    hamming_distance,
    normalize_text,
    simhash64,
)


class NormalizeTest(unittest.TestCase):
    """文本规范化。"""

    def test_strips_at_punctuation_and_emoji(self) -> None:
        self.assertEqual(normalize_text("@麦麦 你 好！！！😊"), "你好")

    def test_keeps_alnum_and_collapses_runs(self) -> None:
        self.assertEqual(normalize_text("哈哈哈哈哈 真的吗？？？"), "哈哈真的吗")

    def test_empty_input(self) -> None:
        self.assertEqual(normalize_text(""), "")
        self.assertEqual(normalize_text("！！！？？？"), "")


class SimHashTest(unittest.TestCase):
    """指纹与相似度。

    SimHash 在本插件里只承担"逐字重复"的快速判定（距离 0 即同一句），
    中文近义改写交给 2-gram Jaccard / 包含度，测试按这个分工来断言。
    """

    def test_identical_normalized_text_has_zero_distance(self) -> None:
        left = normalize_text("！！！今天 天气 不错😊")
        right = normalize_text("今天天气不错")
        self.assertEqual(left, right)
        self.assertEqual(hamming_distance(simhash64(left), simhash64(right)), 0)

    def test_paraphrase_is_caught_by_ngram_metrics(self) -> None:
        base = normalize_text("今晚一起打游戏吗我这边随时可以")
        para = normalize_text("今晚一起打游戏吗？我这边随时都可以！")
        self.assertGreaterEqual(bigram_similarity(base, para), 0.72)
        self.assertGreaterEqual(bigram_containment(base, para), 0.85)

    def test_unrelated_texts_are_far(self) -> None:
        left = normalize_text("今天天气不错，我们去楼下走走吧")
        right = normalize_text("服务器磁盘占用告警请运维同学处理")
        self.assertGreater(hamming_distance(simhash64(left), simhash64(right)), 6)
        self.assertEqual(bigram_similarity(left, right), 0.0)


class GuardFlowTest(unittest.TestCase):
    """判定状态机。"""

    def make_guard(self, **overrides: object) -> StreamGuard:
        params = GuardParams()
        for key, value in overrides.items():
            setattr(params, key, value)
        return StreamGuard(params)

    def test_human_message_resets_chain(self) -> None:
        guard = self.make_guard()
        guard.evaluate(now=1.0, uid="bot-a", is_bot=True, text="第一条机器人消息内容")
        self.assertEqual(guard.chain_length, 1)
        guard.evaluate(now=2.0, uid="human", is_bot=False, text="人类插话")
        self.assertEqual(guard.chain_length, 0)

    def test_chain_limit_blocks_fourth_bot_message(self) -> None:
        guard = self.make_guard()
        texts = ["群里的朋友们大家早上好", "今天有什么值得聊的事情", "我先来问一句吃饭了没", "那我换一个话题聊聊"]
        results = [
            guard.evaluate(now=float(index), uid=f"bot-{index}", is_bot=True, text=text)
            for index, text in enumerate(texts, start=1)
        ]
        self.assertTrue(results[0].allow)
        self.assertFalse(results[-1].allow)
        self.assertEqual(results[-1].reason, "chain_limit")

    def test_duplicate_from_second_bot_is_blocked(self) -> None:
        guard = self.make_guard()
        first = guard.evaluate(now=1.0, uid="bot-a", is_bot=True, text="今晚一起打游戏吗我这边随时可以")
        second = guard.evaluate(now=2.0, uid="bot-b", is_bot=True, text="今晚一起打游戏吗？我这边随时都可以！")
        self.assertTrue(first.allow)
        self.assertFalse(second.allow)
        self.assertIn(second.reason, {"duplicate", "pingpong", "chain_limit"})
        self.assertTrue(second.duplicate or second.pingpong)

    def test_self_echo_is_dropped_without_strike(self) -> None:
        guard = self.make_guard()
        decision = guard.evaluate(now=1.0, uid="me", is_bot=True, text="我自己发出去的消息", is_self=True)
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, "self_echo")
        self.assertEqual(guard.strike, 0)

    def test_cooldown_backoff_and_recovery(self) -> None:
        guard = self.make_guard(cooldown_base_seconds=100, cooldown_multiplier=2, cooldown_max_seconds=1000)
        texts = ["第一条内容完全不同的机器人消息", "第二条内容也互不相同的消息", "第三条又是全新的表达方式", "第四条凑满链条"]
        for index, text in enumerate(texts, start=1):
            guard.evaluate(now=float(index), uid=f"bot-{index}", is_bot=True, text=text)
        self.assertGreater(guard.cooldown_remaining(10.0), 0)
        blocked = guard.evaluate(now=10.0, uid="bot-x", is_bot=True, text="这是冷却期内的一条新消息")
        self.assertFalse(blocked.allow)
        self.assertEqual(blocked.reason, "cooldown")
        guard.evaluate(now=11.0, uid="human", is_bot=False, text="人类出来说句话")
        self.assertEqual(guard.cooldown_remaining(11.0), 0)

    def test_burst_detection(self) -> None:
        guard = self.make_guard(burst_count=3, burst_window_seconds=30, max_chain_length=10)
        guard.evaluate(now=1.0, uid="bot-a", is_bot=True, text="第一条不同的机器人发言")
        guard.evaluate(now=2.0, uid="bot-b", is_bot=True, text="第二条不同的机器人发言")
        third = guard.evaluate(now=3.0, uid="bot-c", is_bot=True, text="第三条又是不同的机器人发言")
        self.assertTrue(third.burst)

    def test_looks_like_bot_echo_detects_unregistered_copy(self) -> None:
        guard = self.make_guard()
        guard.evaluate(now=1.0, uid="bot-a", is_bot=True, text="今天的活动安排已经发到群里了大家记得看")
        self.assertTrue(
            guard.looks_like_bot_echo(now=2.0, uid="unknown", text="今天的活动安排已经发到群里了，大家记得看一下")
        )
        self.assertFalse(guard.looks_like_bot_echo(now=2.0, uid="unknown", text="我要去买杯咖啡稍后回来"))

    def test_looks_like_bot_echo_covers_unknown_to_unknown(self) -> None:
        guard = self.make_guard()
        guard.evaluate(now=1.0, uid="unknown-a", is_bot=False, text="下午三点在会议室开个短会记得带材料")
        self.assertTrue(guard.looks_like_bot_echo(now=5.0, uid="unknown-b", text="下午三点在会议室开个短会，记得带材料。"))

    def test_looks_like_bot_echo_respects_time_and_length(self) -> None:
        guard = self.make_guard()
        guard.evaluate(now=1.0, uid="unknown-a", is_bot=False, text="下午三点在会议室开个短会记得带材料")
        self.assertFalse(
            guard.looks_like_bot_echo(now=120.0, uid="unknown-b", text="下午三点在会议室开个短会，记得带材料。")
        )
        guard.evaluate(now=121.0, uid="unknown-a", is_bot=False, text="哈哈哈哈")
        self.assertFalse(guard.looks_like_bot_echo(now=122.0, uid="unknown-b", text="哈哈哈哈"))

    def test_outgoing_duplicate_is_blocked(self) -> None:
        guard = self.make_guard()
        guard.evaluate(now=1.0, uid="bot-a", is_bot=True, text="群里有人提到明天的会议安排吗")
        decision = guard.check_outgoing(now=2.0, text="群里有人提到明天的会议安排吗？", self_uid="me")
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, "duplicate_outgoing")

    def test_outgoing_cooldown_blocks_even_new_text(self) -> None:
        guard = self.make_guard(cooldown_base_seconds=60)
        texts = ["第一条内容完全不同的机器人消息", "第二条内容也互不相同的消息", "第三条又是全新的表达方式", "第四条凑满链条"]
        for index, text in enumerate(texts, start=1):
            guard.evaluate(now=float(index), uid=f"bot-{index}", is_bot=True, text=text)
        decision = guard.check_outgoing(now=10.0, text="一句全新的、毫无重复的回复内容", self_uid="me")
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, "cooldown")

    def test_outgoing_counts_into_chain(self) -> None:
        guard = self.make_guard(max_chain_length=4)
        self.assertTrue(guard.evaluate(now=1.0, uid="bot-a", is_bot=True, text="第一条机器人消息内容").allow)
        self.assertTrue(guard.check_outgoing(now=2.0, text="本实例的第一条回复内容", self_uid="me").allow)
        self.assertEqual(guard.chain_length, 2)
        self.assertTrue(guard.evaluate(now=3.0, uid="bot-a", is_bot=True, text="第二条机器人消息内容").allow)
        decision = guard.check_outgoing(now=4.0, text="本实例的第二条回复内容", self_uid="me")
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, "chain_limit")

    def test_senders_lists_verdicts(self) -> None:
        guard = self.make_guard()
        guard.evaluate(now=1.0, uid="bot-a", is_bot=True, text="机器人甲说了一句话")
        guard.evaluate(now=2.0, uid="human-1", is_bot=False, text="人类插了一句嘴")
        guard.evaluate(now=3.0, uid="bot-a", is_bot=True, text="机器人甲又说了一句")
        senders = guard.senders(4.0)
        self.assertEqual(len(senders), 2)
        by_uid = {item["uid"]: item for item in senders}
        self.assertTrue(by_uid["bot-a"]["is_bot"])
        self.assertFalse(by_uid["human-1"]["is_bot"])
        self.assertEqual(by_uid["bot-a"]["count"], 2)

    def test_arbiter_yields_position(self) -> None:
        params = GuardParams(arbiter_enabled=True, max_chain_length=4)
        primary = StreamGuard(params)
        secondary = StreamGuard(params)
        for guard, primary_flag in ((primary, True), (secondary, False)):
            guard.evaluate(
                now=1.0, uid="bot-a", is_bot=True, text="今晚一起打游戏吗我这边随时可以", is_arbiter_primary=primary_flag
            )
            guard.evaluate(
                now=2.0, uid="bot-b", is_bot=True, text="今晚一起打游戏吗？我这边随时都可以！", is_arbiter_primary=primary_flag
            )
        self.assertGreaterEqual(secondary.suppressed, primary.suppressed)


class FrequencySuspectTest(unittest.TestCase):
    """频率嫌疑：高频 + 响应式发言，而不是单纯的发言频率。"""

    def make_guard(self, **overrides: object) -> StreamGuard:
        params = GuardParams()
        for key, value in overrides.items():
            setattr(params, key, value)
        return StreamGuard(params)

    def test_human_burst_is_not_suspect(self) -> None:
        # 真人连发 6 句短句，中间没有别人插话：响应式占比接近 0
        guard = self.make_guard()
        for index in range(6):
            guard.evaluate(now=float(index + 1), uid="human", is_bot=False, text="哈哈")
        report = guard.check_frequency(now=7.0, uid="human")
        self.assertEqual(report.count, 6)
        self.assertLess(report.ratio, 0.7)
        self.assertFalse(report.mark)

    def test_bot_pingpong_gets_marked_after_three_windows(self) -> None:
        guard = self.make_guard(suspect_min_messages=3, suspect_strikes_to_mark=3)
        marked_at: list[float] = []
        for window in range(3):
            base = float(window * 90)
            for turn in range(3):
                ts = base + turn * 2
                guard.evaluate(now=ts, uid="other", is_bot=True, text=f"别人说的第 {turn} 句")
                guard.evaluate(now=ts + 1, uid="candidate", is_bot=False, text=f"接话第 {turn} 句")
            report = guard.check_frequency(now=base + 8, uid="candidate")
            if report.mark:
                marked_at.append(base)
        self.assertEqual(len(marked_at), 1, f"应恰好标记一次，实际 {marked_at}")

    def test_one_burst_only_counts_once(self) -> None:
        guard = self.make_guard(suspect_min_messages=3)
        for turn in range(3):
            ts = float(turn * 2)
            guard.evaluate(now=ts, uid="other", is_bot=True, text=f"别人 {turn}")
            guard.evaluate(now=ts + 1, uid="candidate", is_bot=False, text=f"接话 {turn}")
        first = guard.check_frequency(now=8.0, uid="candidate")
        second = guard.check_frequency(now=9.0, uid="candidate")
        self.assertEqual(first.strikes, 1)
        self.assertEqual(second.strikes, 1, "同一波高频不应重复记嫌疑")

    def test_low_volume_is_ignored(self) -> None:
        guard = self.make_guard()
        guard.evaluate(now=1.0, uid="other", is_bot=False, text="别人说话")
        guard.evaluate(now=2.0, uid="candidate", is_bot=False, text="接一句")
        report = guard.check_frequency(now=3.0, uid="candidate")
        self.assertEqual(report.count, 1)
        self.assertFalse(report.suspect)

    def test_record_false_does_not_accumulate(self) -> None:
        guard = self.make_guard(suspect_min_messages=3)
        for turn in range(3):
            ts = float(turn * 2)
            guard.evaluate(now=ts, uid="other", is_bot=True, text=f"别人 {turn}")
            guard.evaluate(now=ts + 1, uid="candidate", is_bot=False, text=f"接话 {turn}")
        before = len(guard._suspect_strikes)
        peek = guard.check_frequency(now=8.0, uid="candidate", record=False)
        self.assertEqual(len(guard._suspect_strikes), before, "只读查询不应写入嫌疑计数")
        self.assertTrue(peek.suspect)
        self.assertFalse(peek.mark)


class RegistryTest(unittest.TestCase):
    """多流管理与快照。"""

    def test_snapshot_and_restore(self) -> None:
        registry = GuardRegistry(GuardParams())
        guard = registry.get("stream-1", 1.0)
        guard.evaluate(now=1.0, uid="bot-a", is_bot=True, text="一条会被记录的机器人消息")
        snapshot = registry.snapshot(1.5)
        self.assertIn("stream-1", snapshot)

        restored = GuardRegistry(GuardParams())
        restored.restore(snapshot, 2.0)
        restored_guard = restored.get("stream-1", 2.0)
        self.assertEqual(restored_guard.chain_length, 0, "重启时无指纹依据，仅恢复冷却及统计")
        self.assertEqual(restored_guard.suppressed, 0)

    def test_reset_clears_streams(self) -> None:
        registry = GuardRegistry(GuardParams())
        registry.get("stream-1", 1.0).evaluate(now=1.0, uid="bot-a", is_bot=True, text="内容一")
        self.assertEqual(registry.reset(), ["stream-1"])
        self.assertEqual(registry.reset(), [])


if __name__ == "__main__":
    unittest.main()
