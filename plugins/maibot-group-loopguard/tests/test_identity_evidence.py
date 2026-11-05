"""身份识别、频率统计与过期边界的纯标准库回归。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loopguard_core import GuardParams, GuardRegistry, StreamGuard

SAMPLES = [
    "今天下午三点在会议室开个短会记得带材料",
    "周末准备去海边看看日落顺便拍一组照片",
    "服务器版本已经更新请各位重新启动客户端",
    "新买的机械键盘打字手感很好准备带到办公室",
]


class IdentityEvidenceTest(unittest.TestCase):
    def test_single_human_copy_is_only_suspect(self):
        guard = StreamGuard()
        guard.evaluate(now=1, uid="human-a", is_bot=False, text=SAMPLES[0])
        guard.evaluate(now=2, uid="human-b", is_bot=False, text=SAMPLES[0])
        report = guard.identity_report(now=2, uid="human-b")
        self.assertTrue(report.suspect)
        self.assertEqual(report.echoes, 1)
        self.assertFalse(report.mark)

    def test_three_anchored_echoes_mark_next_turn(self):
        guard = StreamGuard()
        for index, text in enumerate(SAMPLES[:3]):
            guard.evaluate(now=index * 3, uid="known", is_bot=True, text=text)
            guard.evaluate(now=index * 3 + 1, uid="candidate", is_bot=False, text=text)
            report = guard.identity_report(now=index * 3 + 1, uid="candidate")
            self.assertEqual(report.mark, index == 2)
        self.assertEqual(report.bot_responses, 3)
        self.assertEqual(report.distinct_echoes, 3)

    def test_unknown_echoes_need_extra_round(self):
        guard = StreamGuard()
        for index, text in enumerate(SAMPLES):
            guard.evaluate(now=index * 3, uid="other", is_bot=False, text=text)
            guard.evaluate(now=index * 3 + 1, uid="candidate", is_bot=False, text=text)
            self.assertEqual(guard.identity_report(now=index * 3 + 1, uid="candidate").mark, index == 3)

    def test_slow_model_echoes_use_separate_configurable_interval(self):
        guard = StreamGuard(GuardParams(identity_window_seconds=300))
        for index, text in enumerate(SAMPLES[:3]):
            guard.evaluate(now=index * 70, uid="known", is_bot=True, text=text)
            guard.evaluate(now=index * 70 + 40, uid="candidate", is_bot=False, text=text)
        self.assertTrue(guard.identity_report(now=181, uid="candidate").mark)
        self.assertEqual(guard.check_frequency(now=181, uid="candidate", record=False).bot_responsive, 0)

    def test_repeated_slogan_is_not_independent_evidence(self):
        guard = StreamGuard()
        for index in range(6):
            guard.evaluate(now=index * 2, uid="other", is_bot=True, text=SAMPLES[0])
            guard.evaluate(now=index * 2 + 1, uid="candidate", is_bot=False, text=SAMPLES[0])
        report = guard.identity_report(now=12, uid="candidate")
        self.assertEqual(report.distinct_echoes, 1)
        self.assertFalse(report.mark)

    def test_short_source_inside_long_message_is_not_echo(self):
        guard = StreamGuard()
        guard.evaluate(now=1, uid="other", is_bot=True, text="今天")
        self.assertFalse(guard.looks_like_bot_echo(now=2, uid="candidate", text=SAMPLES[0]))
        guard.evaluate(now=3, uid="other", is_bot=True, text=SAMPLES[0])
        self.assertFalse(guard.looks_like_bot_echo(now=4, uid="candidate", text=SAMPLES[0] + SAMPLES[1] + SAMPLES[2]))

    def test_expired_identity_evidence_does_not_mark(self):
        guard = StreamGuard(GuardParams(identity_window_seconds=10))
        for index, text in enumerate(SAMPLES[:3]):
            now = index * 20
            guard.evaluate(now=now, uid="known", is_bot=True, text=text)
            guard.evaluate(now=now + 1, uid="candidate", is_bot=False, text=text)
        report = guard.identity_report(now=42, uid="candidate")
        self.assertEqual(report.echoes, 1)
        self.assertFalse(report.mark)

    def test_forget_clears_evidence_and_frequency_windows(self):
        guard = StreamGuard()
        for index, text in enumerate(SAMPLES[:3]):
            guard.evaluate(now=index * 2, uid="known", is_bot=True, text=text)
            guard.evaluate(now=index * 2 + 1, uid="candidate", is_bot=False, text=text)
        self.assertTrue(guard.identity_report(now=6, uid="candidate").mark)
        guard.forget_identity("candidate")
        self.assertFalse(guard.identity_report(now=6, uid="candidate").suspect)

    def test_sender_verdict_uses_latest_record_not_historical_or(self):
        guard = StreamGuard()
        guard.evaluate(now=1, uid="candidate", is_bot=True, text=SAMPLES[0])
        guard.evaluate(now=2, uid="candidate", is_bot=False, text=SAMPLES[1])
        self.assertFalse(guard.senders(2)[0]["is_bot"])


class FrequencyIsolationTest(unittest.TestCase):
    def make_guard(self, **kwargs):
        return StreamGuard(GuardParams(suspect_min_messages=3, **kwargs))

    def wave(self, guard, base, uid, *, known=True):
        for turn in range(3):
            guard.evaluate(now=base + turn * 2, uid="anchor", is_bot=known, text=f"锚点{turn}")
            guard.evaluate(now=base + turn * 2 + 1, uid=uid, is_bot=False, text=f"接话{turn}")
        return guard.check_frequency(now=base + 6, uid=uid)

    def test_one_other_message_then_self_burst_counts_one_response(self):
        guard = self.make_guard()
        guard.evaluate(now=0, uid="other", is_bot=True, text="起个头")
        for now in range(1, 7):
            guard.evaluate(now=now, uid="human", is_bot=False, text="连发短句")
        report = guard.check_frequency(now=7, uid="human")
        self.assertEqual(report.responsive, 1)
        self.assertFalse(report.suspect)
        self.assertFalse(report.mark)

    def test_two_humans_chatting_never_gain_bot_windows(self):
        guard = self.make_guard()
        for base in (0, 90, 180, 270):
            report = self.wave(guard, base, "human", known=False)
            self.assertTrue(report.suspect)
            self.assertEqual(report.strikes, 0)
            self.assertFalse(report.mark)

    def test_accounts_do_not_share_strikes_or_window_throttles(self):
        guard = self.make_guard()
        first = self.wave(guard, 0, "alice")
        second = self.wave(guard, 10, "bob")
        self.assertEqual(first.strikes, 1)
        self.assertEqual(second.strikes, 1)
        self.assertFalse(second.mark)
        third = self.wave(guard, 90, "alice")
        self.assertEqual(third.strikes, 2)
        self.assertEqual(guard.check_frequency(now=96, uid="bob", record=False).strikes, 1)

    def test_expired_strikes_are_hidden_even_at_low_volume(self):
        guard = self.make_guard(suspect_strike_ttl_seconds=20)
        self.assertEqual(self.wave(guard, 0, "candidate").strikes, 1)
        self.assertEqual(guard.check_frequency(now=40, uid="candidate", record=False).strikes, 0)

    def test_read_only_does_not_mark_or_write_windows(self):
        guard = self.make_guard()
        self.wave(guard, 0, "candidate")
        before = {uid: list(values) for uid, values in guard._suspect_strikes.items()}
        report = guard.check_frequency(now=8, uid="candidate", record=False)
        self.assertEqual(before, {uid: list(values) for uid, values in guard._suspect_strikes.items()})
        self.assertFalse(report.mark)

    def test_equal_timestamps_respect_arrival_order(self):
        guard = self.make_guard()
        guard.evaluate(now=1, uid="other", is_bot=True, text="锚点")
        for _ in range(5):
            guard.evaluate(now=1, uid="human", is_bot=False, text="连发")
        self.assertEqual(guard.check_frequency(now=1, uid="human").responsive, 1)

    def test_longer_frequency_window_survives_short_detect_window(self):
        guard = self.make_guard(window_seconds=10, suspect_window_seconds=60)
        for index in range(3):
            guard.evaluate(now=index * 15, uid="known", is_bot=True, text="锚点")
            guard.evaluate(now=index * 15 + 1, uid="candidate", is_bot=False, text="接话")
        self.assertEqual(guard.check_frequency(now=32, uid="candidate").count, 3)

    def test_same_messages_do_not_count_twice_at_window_boundary(self):
        guard = self.make_guard()
        self.wave(guard, 0, "candidate")
        report = guard.check_frequency(now=66, uid="candidate")
        self.assertLessEqual(report.strikes, 1)


class StateBoundaryTest(unittest.TestCase):
    def test_platform_scoped_registry_isolates_cooldown_and_reset(self):
        registry = GuardRegistry()
        qq = registry.get("same", 1, platform="qq")
        tg = registry.get("same", 1, platform="telegram")
        for now in (1, 2):
            qq.evaluate(now=now, uid="b", is_bot=True, text=SAMPLES[0])
        self.assertGreater(qq.cooldown_remaining(2), 0)
        self.assertEqual(tg.cooldown_remaining(2), 0)
        with self.assertRaises(ValueError):
            registry.get("same", 2)
        registry.reset("qq:same")
        self.assertIs(registry.get("same", 2), tg)

    def test_restart_restores_cooldown_but_not_unverifiable_chain(self):
        guard = StreamGuard()
        for now in (1, 2, 3):
            guard.evaluate(now=now, uid="known", is_bot=True, text="")
        restored = StreamGuard()
        restored.restore(guard.snapshot(3), 4)
        result = restored.evaluate(now=5, uid="known", is_bot=True, text=SAMPLES[0])
        self.assertTrue(result.allow)
        self.assertEqual(result.chain_length, 1)
        restored.restore({"cooldown_remaining": 60}, 6)
        self.assertFalse(restored.evaluate(now=7, uid="known", is_bot=True, text=SAMPLES[1]).allow)

    def test_old_chain_expires_without_human_message(self):
        guard = StreamGuard()
        for now in (1, 2, 3):
            guard.evaluate(now=now, uid="known", is_bot=True, text="")
        result = guard.evaluate(now=1000, uid="known", is_bot=True, text=SAMPLES[0])
        self.assertTrue(result.allow)
        self.assertEqual(guard.chain_length, 1)

    def test_outgoing_duplicate_expires(self):
        guard = StreamGuard()
        self.assertTrue(guard.check_outgoing(now=1, self_uid="self", text=SAMPLES[0]).allow)
        self.assertTrue(guard.check_outgoing(now=1000, self_uid="self", text=SAMPLES[0]).allow)

    def test_non_alternating_two_senders_not_pingpong(self):
        guard = StreamGuard(GuardParams(max_chain_length=20))
        for now, uid in enumerate(("a", "a", "b", "b")):
            result = guard.evaluate(now=now, uid=uid, is_bot=True, text="")
        self.assertFalse(result.pingpong)

    def test_self_echo_does_not_duplicate_outgoing_observation(self):
        guard = StreamGuard()
        guard.check_outgoing(now=1, self_uid="self", text=SAMPLES[0])
        guard.evaluate(now=2, uid="self", is_bot=True, is_self=True, text=SAMPLES[0])
        self.assertEqual(guard.snapshot(2)["window_records"], 1)


if __name__ == "__main__":
    unittest.main()
