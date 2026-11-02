"""双机器人同群收敛模拟。

两只机器人各自装一份复读守卫，跑两个场景：

- 场景 A：互相复述（只改标点）—— 出站闸门在第 1 次回声就刹住；
- 场景 B：内容各不相同、但节奏成环 —— 入站闸门在第 3~4 条按链条/交替拦住。

运行：

    python tools/loop_sim.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "maibot-group-loopguard"))

from loopguard_core import GuardParams, StreamGuard  # noqa: E402

HUMAN = "10000"


class Bot:
    """一只装了守卫的机器人。"""

    def __init__(self, name: str, account: str, *, verbose: bool = True) -> None:
        self.name = name
        self.account = account
        self.guard = StreamGuard(GuardParams())
        self.verbose = verbose
        self.sent: list[str] = []

    def receive(self, now: float, sender: str, text: str, *, is_bot: bool) -> bool:
        """入站判定：返回 True 表示消息进入主链，可以参与回复。"""

        decision = self.guard.evaluate(
            now=now,
            uid=sender,
            is_bot=is_bot,
            text=text,
            is_self=sender == self.account,
            is_arbiter_primary=True,
        )
        if self.verbose:
            self._log(now, "收", sender, text, decision.allow, decision.reason)
        return decision.allow

    def send(self, now: float, text: str) -> bool:
        """出站判定：返回 True 表示真的发出去了。"""

        decision = self.guard.check_outgoing(now=now, text=text, self_uid=self.account)
        if self.verbose:
            self._log(now, "发", self.account, text, decision.allow, decision.reason)
        if decision.allow:
            self.sent.append(text)
        return decision.allow

    def _log(self, now: float, action: str, who: str, text: str, allow: bool, reason: str) -> None:
        verdict = "放行" if allow else f"拦截/{reason}"
        print(f"{now:>6.1f}s  {self.name:<5} {action} {who:<8} {verdict:<22} {text}")


def _header(title: str) -> None:
    print()
    print("=" * 100)
    print(title)
    print("=" * 100)


def scenario_duplicate() -> None:
    """场景 A：内容几乎逐字相同，应由出站闸门拦下。"""

    _header("场景 A：两只机器人互相复述（只改标点）")
    a, b = Bot("Bot-A", "20001"), Bot("Bot-B", "20002")
    human_text = "今晚有人开黑吗"
    print(f"{0.0:>6.1f}s  {'人类':<5} 发 {HUMAN:<8} {'—':<22} {human_text}")

    a.receive(1.0, HUMAN, human_text, is_bot=False)
    b.receive(1.2, HUMAN, human_text, is_bot=False)

    script = [
        (a, 2.0, "有的有的，我随时可以"),
        (b, 3.0, "那我也来，几点开始"),
        (a, 4.0, "那我也来，几点开始？"),
        (b, 5.0, "那我也来，几点开始？？"),
    ]
    for bot, now, text in script:
        # 每条回复先过对方的入站闸门，再由发送方走出站闸门
        peer = b if bot is a else a
        peer.receive(now - 0.5, bot.account, text, is_bot=True)
        bot.send(now, text)

    print("-" * 100)
    print(f"Bot-A 实际发出 {len(a.sent)} 条，累计拦截 {a.guard.suppressed} 次")
    print(f"Bot-B 实际发出 {len(b.sent)} 条，累计拦截 {b.guard.suppressed} 次")


def scenario_pingpong() -> None:
    """场景 B：内容各不相同，但节奏成环，应由入站闸门按链条/交替拦下。"""

    _header("场景 B：内容各异但节奏成环（两只机器人来回接话）")
    a, b = Bot("Bot-A", "30001"), Bot("Bot-B", "30002")
    human_text = "周末有什么安排吗"
    print(f"{0.0:>6.1f}s  {'人类':<5} 发 {HUMAN:<8} {'—':<22} {human_text}")

    a.receive(1.0, HUMAN, human_text, is_bot=False)
    b.receive(1.2, HUMAN, human_text, is_bot=False)

    turns = [
        (a, 2.0, "我打算去爬山，顺便拍点照片"),
        (b, 3.0, "爬山听起来不错，我可能去逛展"),
        (a, 4.0, "逛展也行，最近有几个摄影展"),
        (b, 5.0, "摄影展我倒是没关注过，有推荐吗"),
        (a, 6.0, "我回头整理一份清单发群里"),
        (b, 7.0, "好啊，那我等你的清单"),
    ]
    for bot, now, text in turns:
        peer = b if bot is a else a
        peer.receive(now - 0.5, bot.account, text, is_bot=True)
        bot.send(now, text)

    print("-" * 100)
    print(f"Bot-A 实际发出 {len(a.sent)} 条，累计拦截 {a.guard.suppressed} 次，冷却 {a.guard.cooldown_remaining(7.0):.0f}s")
    print(f"Bot-B 实际发出 {len(b.sent)} 条，累计拦截 {b.guard.suppressed} 次，冷却 {b.guard.cooldown_remaining(7.0):.0f}s")


def scenario_human_recovery() -> None:
    """场景 C：人类插话后恢复正常。"""

    _header("场景 C：人类插话把发言权还回来")
    a = Bot("Bot-A", "40001")
    a.receive(1.0, HUMAN, "周末有什么安排吗", is_bot=False)
    for index, text in enumerate(["我打算去爬山", "爬山听起来不错", "摄影展我倒是没关注过", "我回头整理清单"], start=1):
        a.receive(float(index + 1), f"5000{index}", text, is_bot=True)
    print(
        f"{'':>6}  Bot-A 连续机器人消息={a.guard.chain_length} 退避档位={a.guard.strike} "
        f"剩余冷却={a.guard.cooldown_remaining(6.0):.0f}s"
    )
    a.receive(20.0, HUMAN, "别吵了，我来定", is_bot=False)
    print(
        f"{'':>6}  人类发言后：连续机器人消息={a.guard.chain_length} 退避档位={a.guard.strike} "
        f"剩余冷却={a.guard.cooldown_remaining(20.0):.0f}s"
    )


def main() -> None:
    """依次跑三个场景。"""

    scenario_duplicate()
    scenario_pingpong()
    scenario_human_recovery()
    print()
    print("结论：入站闸门负责「不接话」，出站闸门负责「说不出」，人类发言负责把状态清零。")


if __name__ == "__main__":
    main()
