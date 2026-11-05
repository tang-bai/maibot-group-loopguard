"""群聊复读守卫核心算法。

本模块只依赖 Python 标准库，刻意与 `maibot_sdk` 解耦：
- 便于脱离 MaiBot 运行时做单元测试与回归；
- 便于在其它宿主（AstrBot / NoneBot）中复用同一套判定逻辑。

对外只暴露三类对象：
- `normalize_text` / `simhash64` / `hamming_distance`：文本指纹工具；
- `GuardParams`：全部阈值参数（由插件配置映射而来）；
- `StreamGuard`：单聊天流状态机，负责 `evaluate` / `check_outgoing` / 快照。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Iterable, Optional

__all__ = [
    "GuardParams",
    "Decision",
    "MessageRecord",
    "IdentityReport",
    "FrequencyReport",
    "StreamGuard",
    "normalize_text",
    "simhash64",
    "hamming_distance",
    "bigram_similarity",
    "bigram_containment",
]

# CQ 码、URL、@ 提及在指纹计算前先剔除，避免"同一句话换个 @ 就绕过查重"。
_CQ_RE = re.compile(r"\[CQ:[^\]]*\]")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_AT_RE = re.compile(r"@[^\s@]{1,40}")
_RUN_RE = re.compile(r"(.)\1{2,}", re.DOTALL)
_DIGEST_LEN = 16

# 行为学习的判据：短时间内的逐字/高包含度复述才算"机器人回声"
_ECHO_MIN_LENGTH = 8
_ECHO_CONTAINMENT = 0.9


def _keep_char(ch: str) -> bool:
    """判断字符是否进入指纹：只保留字母与数字（含 CJK）。"""

    category = unicodedata.category(ch)
    return category[0] in {"L", "N"}


def normalize_text(text: str, *, max_length: int = 400) -> str:
    """把原始消息文本压缩成用于查重的规范形式。

    处理顺序：NFKC 规范化 → 去 CQ 码 / URL / @ 提及 → 只保留字母数字 →
    转小写 → 折叠连续重复字符 → 截断。

    Args:
        text: 原始文本。
        max_length: 规范化后的最大长度，超出部分丢弃。

    Returns:
        str: 规范化文本；输入为空时返回空串。
    """

    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", str(text))
    normalized = _CQ_RE.sub(" ", normalized)
    normalized = _URL_RE.sub(" ", normalized)
    normalized = _AT_RE.sub(" ", normalized)
    normalized = "".join(ch for ch in normalized if _keep_char(ch))
    normalized = normalized.lower()
    # "啊啊啊啊啊" 与 "啊啊" 视为同一强度，减少语气词造成的假阴性。
    normalized = _RUN_RE.sub(r"\1\1", normalized)
    return normalized[:max_length]


def _shingles(text: str, size: int) -> dict[str, int]:
    """生成字符 n-gram 及其权重。

    对短文本自动降级为 2-gram；同时把整串作为一个高权重特征加入，
    让"前缀相同、结尾不同"的两条回复也能落在较小汉明距离内。
    """

    if not text:
        return {}
    grams: dict[str, int] = {}
    effective = size if len(text) >= size else max(2, len(text))
    for index in range(len(text) - effective + 1):
        gram = text[index : index + effective]
        grams[gram] = grams.get(gram, 0) + 1
    grams[text] = grams.get(text, 0) + 2
    return grams


def simhash64(text: str, *, shingle: int = 3) -> int:
    """计算 64 位 SimHash。

    Args:
        text: 已经过 `normalize_text` 的文本（未规范化也能算，但建议先规范化）。
        shingle: n-gram 长度。

    Returns:
        int: 64 位无符号整数指纹。
    """

    grams = _shingles(text, shingle)
    if not grams:
        return 0

    vector = [0] * 64
    for gram, weight in grams.items():
        digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
        hashed = int.from_bytes(digest, "big")
        for bit in range(64):
            vector[bit] += weight if (hashed >> bit) & 1 else -weight

    result = 0
    for bit, value in enumerate(vector):
        if value > 0:
            result |= 1 << bit
    return result


def hamming_distance(left: int, right: int) -> int:
    """计算两个 SimHash 的汉明距离。"""

    return bin((left ^ right) & 0xFFFFFFFFFFFFFFFF).count("1")


def _bigrams(text: str) -> set[str]:
    """取字符 2-gram 集合，用于短文本的 Jaccard 兜底比较。"""

    if len(text) < 2:
        return {text} if text else set()
    return {text[index : index + 2] for index in range(len(text) - 1)}


def bigram_similarity(left: str, right: str) -> float:
    """计算两段规范化文本的 2-gram Jaccard 相似度。"""

    left_grams = _bigrams(left)
    right_grams = _bigrams(right)
    if not left_grams or not right_grams:
        return 0.0
    intersection = len(left_grams & right_grams)
    union = len(left_grams | right_grams)
    return intersection / union if union else 0.0


def bigram_containment(left: str, right: str) -> float:
    """计算 2-gram 包含度（重叠系数）。

    机器人复读常见形态是"把上一句抄一遍再补几个字"，此时 Jaccard 会被新增
    内容稀释，包含度仍然很高，因此两个指标一起用。
    """

    left_grams = _bigrams(left)
    right_grams = _bigrams(right)
    if not left_grams or not right_grams:
        return 0.0
    smaller = min(len(left_grams), len(right_grams))
    if smaller == 0:
        return 0.0
    return len(left_grams & right_grams) / smaller


@dataclass(slots=True)
class GuardParams:
    """判定阈值。字段与插件配置一一对应，默认值取"保守可用"档。"""

    window_seconds: float = 180.0
    max_records: int = 60
    min_text_length: int = 4
    simhash_distance: int = 1
    bigram_similarity_threshold: float = 0.72
    bigram_containment_threshold: float = 0.85
    max_chain_length: int = 4
    duplicate_chain_threshold: int = 2
    pingpong_span: int = 6
    pingpong_chain_threshold: int = 3
    burst_count: int = 6
    burst_window_seconds: float = 60.0
    cooldown_base_seconds: float = 120.0
    cooldown_multiplier: float = 2.0
    cooldown_max_seconds: float = 1800.0
    human_reset_seconds: float = 300.0
    strike_decay_on_human: bool = True
    arbiter_enabled: bool = False
    arbiter_yield_threshold: int = 1
    # 频率嫌疑：用"响应式发言比例"区分 bot 接话与真人连发，而不是单看发言频率
    suspect_window_seconds: float = 60.0
    suspect_min_messages: int = 5
    suspect_responsive_ratio: float = 0.7
    suspect_responsive_within_seconds: float = 12.0
    suspect_strikes_to_mark: int = 3
    suspect_strike_ttl_seconds: float = 600.0
    identity_min_echoes: int = 3
    identity_window_seconds: float = 180.0
    identity_min_text_length: int = 12
    identity_response_within_seconds: float = 60.0


@dataclass(slots=True)
class MessageRecord:
    """一条进入判定窗口的消息指纹。"""

    ts: float
    uid: str
    is_bot: bool
    digest: str
    simhash: int
    normalized: str
    preview: str
    responsive: bool = False
    bot_response: bool = False
    echo_source: str = ""
    echo_anchored: bool = False
    platform: str = ""
    nickname: str = ""
    cardname: str = ""
    identity_source: str = ""


@dataclass(slots=True)
class Decision:
    """一次判定的结果。"""

    allow: bool
    reason: str
    duplicate: bool = False
    pingpong: bool = False
    burst: bool = False
    chain_length: int = 0
    score: float = 0.0
    cooldown_seconds: float = 0.0
    cooldown_remaining: float = 0.0
    learned_bot: bool = False


@dataclass(slots=True)
class FrequencyReport:
    """一次频率嫌疑评估的结果。

    只统计"发言次数"会把活跃的真人误判成机器人；真正能把两者分开的是
    **响应式发言比例**：bot 是被触发的，几乎每条都接在别人后面；
    真人连发则是自己一条接一条，中间没有别人插话。
    """

    count: int = 0
    responsive: int = 0
    bot_responsive: int = 0
    ratio: float = 0.0
    bot_ratio: float = 0.0
    strikes: int = 0
    suspect: bool = False
    mark: bool = False


@dataclass(slots=True)
class IdentityReport:
    """行为证据计数；这些是启发式信号，而非真实身份概率。"""

    count: int = 0
    echoes: int = 0
    distinct_echoes: int = 0
    bot_responses: int = 0
    suspect: bool = False
    mark: bool = False
    reasons: tuple[str, ...] = ()


class StreamGuard:
    """单个聊天流（群或私聊）的复读状态机。

    状态包含三部分：
    - `_records`：滑动窗口内的消息指纹，用于查重与节奏分析；
    - `_chain_length`：自最近一条人类消息以来的机器人消息条数；
    - `_strike` / `_cooldown_until`：触发后的指数退避冷却。

    该类不做任何 I/O，线程安全性由调用方保证（插件侧用单事件循环串行调用）。
    """

    def __init__(self, params: Optional[GuardParams] = None) -> None:
        self.params = params or GuardParams()
        self.platform = ""
        self.stream_id = ""
        self._records: Deque[MessageRecord] = deque(maxlen=max(8, self.params.max_records))
        self._chain_length = 0
        self._strike = 0
        self._cooldown_until = 0.0
        self._last_human_ts = 0.0
        self._suppressed = 0
        self._allowed_bot = 0
        self._last_reason = ""
        self._outgoing: Deque[MessageRecord] = deque(maxlen=10)
        self._suspect_strikes: dict[str, Deque[float]] = {}
        self._last_suspect_ts: dict[str, float] = {}

    # ── 状态读取 ────────────────────────────────────────────────
    @property
    def chain_length(self) -> int:
        """当前连续机器人消息条数。"""

        return self._chain_length

    @property
    def strike(self) -> int:
        """当前退避档位。"""

        return self._strike

    @property
    def suppressed(self) -> int:
        """累计拦截次数。"""

        return self._suppressed

    @property
    def last_reason(self) -> str:
        """最近一次拦截原因。"""

        return self._last_reason

    def cooldown_remaining(self, now: float) -> float:
        """剩余冷却秒数。"""

        return max(0.0, self._cooldown_until - now)

    def snapshot(self, now: float) -> dict[str, object]:
        """导出可序列化的状态快照。"""

        return {
            "chain_length": self._chain_length,
            "strike": self._strike,
            "cooldown_remaining": round(self.cooldown_remaining(now), 3),
            "suppressed": self._suppressed,
            "allowed_bot": self._allowed_bot,
            "last_reason": self._last_reason,
            "window_records": len(self._records),
        }

    def restore(self, data: dict[str, object], now: float) -> None:
        """从快照恢复关键状态（窗口内指纹不持久化）。"""

        # 指纹未持久化，重启只恢复冷却/统计，不恢复缺少消息依据的链条。
        self._chain_length = 0
        self._strike = int(data.get("strike") or 0)
        remaining = float(data.get("cooldown_remaining") or 0.0)
        self._cooldown_until = now + max(0.0, remaining)
        self._suppressed = int(data.get("suppressed") or 0)
        self._allowed_bot = int(data.get("allowed_bot") or 0)
        self._last_reason = str(data.get("last_reason") or "")

    # ── 内部工具 ────────────────────────────────────────────────
    def _prune(self, now: float) -> None:
        """丢弃窗口外的历史指纹。"""

        horizon = now - max(1.0, self.params.window_seconds, self.params.identity_window_seconds, self.params.suspect_window_seconds)
        while self._records and self._records[0].ts < horizon:
            self._records.popleft()
        while self._outgoing and self._outgoing[0].ts < now - max(1.0, self.params.window_seconds):
            self._outgoing.popleft()
        if not self._records or self._records[-1].ts < now - max(1.0, self.params.window_seconds):
            self._chain_length = 0
        ttl = max(10.0, self.params.suspect_strike_ttl_seconds)
        for uid in list(self._suspect_strikes):
            strikes = self._suspect_strikes[uid]
            while strikes and now - strikes[0] > ttl:
                strikes.popleft()
            if not strikes:
                self._suspect_strikes.pop(uid, None)
                self._last_suspect_ts.pop(uid, None)

    def _recent_bot_records(self, now: float, window: float) -> list[MessageRecord]:
        """取时间窗内的机器人消息。"""

        horizon = now - max(1.0, window)
        return [record for record in self._records if record.is_bot and record.ts >= horizon]

    @staticmethod
    def _make_record(ts: float, uid: str, is_bot: bool, text: str) -> MessageRecord:
        """构造一条指纹记录。"""

        normalized = normalize_text(text)
        digest = hashlib.blake2b(normalized.encode("utf-8"), digest_size=_DIGEST_LEN).hexdigest()
        return MessageRecord(
            ts=ts,
            uid=str(uid or ""),
            is_bot=bool(is_bot),
            digest=digest,
            simhash=simhash64(normalized),
            normalized=normalized,
            preview=str(text or "")[:80],
        )

    def _matches_any(self, candidate: MessageRecord, pool: Iterable[MessageRecord]) -> bool:
        """判断候选文本是否与池中某条历史机器人消息构成"重复"。"""

        if len(candidate.normalized) < self.params.min_text_length:
            return False
        for record in pool:
            if len(record.normalized) < self.params.min_text_length:
                continue
            if record.digest == candidate.digest:
                return True
            if hamming_distance(candidate.simhash, record.simhash) <= self.params.simhash_distance:
                return True
            if bigram_similarity(candidate.normalized, record.normalized) >= self.params.bigram_similarity_threshold:
                return True
            if bigram_containment(candidate.normalized, record.normalized) >= self.params.bigram_containment_threshold:
                return True
        return False

    def _pingpong(self, now: float) -> bool:
        """检测"两个机器人互相接话"的交替节奏。"""

        span = max(4, self.params.pingpong_span)
        tail = [record for record in self._records if record.ts >= now - max(1.0, self.params.window_seconds)][-span:]
        if len(tail) < 4:
            return False
        if not all(record.is_bot for record in tail):
            return False
        senders = [record.uid for record in tail]
        return len(set(senders)) == 2 and all(left != right for left, right in zip(senders, senders[1:]))

    def _burst(self, now: float) -> bool:
        """检测短时间内机器人消息爆发。"""

        window = max(5.0, self.params.burst_window_seconds)
        count = len(self._recent_bot_records(now, window))
        return count >= max(2, self.params.burst_count)

    def _register_strike(self, now: float) -> float:
        """累加退避档位并计算本次冷却时长。"""

        self._strike = min(self._strike + 1, 8)
        base = max(1.0, self.params.cooldown_base_seconds)
        factor = max(1.0, self.params.cooldown_multiplier)
        duration = min(base * (factor ** (self._strike - 1)), max(base, self.params.cooldown_max_seconds))
        self._cooldown_until = max(self._cooldown_until, now) + duration
        return duration

    # ── 主判定 ──────────────────────────────────────────────────
    def check_frequency(self, *, now: float, uid: str, record: bool = True) -> FrequencyReport:
        """按账号累计独立窗口；接话只看紧邻前驱，且要求已知 bot 锚点。

        普通真人相互聊天即便很快也只给 suspect，不自动标记。
        record=False 只读证据，不新增嫌疑窗口或触发标记。
        """

        self._prune(now)
        params = self.params
        uid = str(uid or "")
        window = max(10.0, params.suspect_window_seconds)
        messages = [item for item in self._records if item.uid == uid and not item.is_bot and 0 <= now - item.ts <= window]
        strikes = self._suspect_strikes.get(uid, ())
        report = FrequencyReport(count=len(messages), strikes=len(strikes))
        report.responsive = sum(item.responsive for item in messages)
        report.bot_responsive = sum(item.bot_response for item in messages)
        if not messages:
            return report
        report.ratio = report.responsive / report.count
        report.bot_ratio = report.bot_responsive / report.count
        if not uid or report.count < max(2, params.suspect_min_messages):
            return report
        if report.ratio < params.suspect_responsive_ratio:
            return report
        report.suspect = True
        # 频率与快速回复都不是身份凭据。只有多轮响应已知 bot 才积累标记证据。
        anchored = report.bot_responsive >= 2 and report.bot_ratio >= params.suspect_responsive_ratio
        if not anchored or not record:
            return report
        if now - self._last_suspect_ts.get(uid, float("-inf")) < window:
            return report
        # 不允许同一批消息在滑动窗口边界重复贡献证据。
        fresh = [item for item in messages if item.ts > self._last_suspect_ts.get(uid, float("-inf"))]
        if len(fresh) < max(2, params.suspect_min_messages):
            return report
        self._last_suspect_ts[uid] = now
        bucket = self._suspect_strikes.setdefault(uid, deque(maxlen=max(8, params.suspect_strikes_to_mark)))
        bucket.append(now)
        report.strikes = len(bucket)
        report.mark = report.strikes >= max(1, params.suspect_strikes_to_mark)
        return report

    def identity_report(self, *, now: float, uid: str) -> IdentityReport:
        """对已观察的消息做多轮回声评估，不因一次复制/昵称/@直接定性。"""

        self._prune(now)
        window = max(10.0, self.params.identity_window_seconds)
        messages = [item for item in self._records if item.uid == uid and not item.is_bot and 0 <= now - item.ts <= window]
        echoes = [item for item in messages if item.echo_source]
        report = IdentityReport(
            count=len(messages),
            echoes=len(echoes),
            distinct_echoes=len({item.echo_source for item in echoes}),
            bot_responses=sum(item.bot_response for item in messages),
        )
        minimum = max(3, self.params.identity_min_echoes)
        anchored_echoes = sum(item.echo_anchored for item in echoes)
        # 优先使用已知机器人的不同内容作锚点；全未知场景需要更多独立回声。
        anchored = report.echoes >= minimum and anchored_echoes >= 2 and report.distinct_echoes >= 2
        unanchored = report.echoes >= minimum + 1 and report.distinct_echoes >= 3
        report.mark = anchored or unanchored
        report.suspect = bool(echoes)
        reasons = []
        if echoes:
            reasons.append(f"回声{report.echoes}次/不同来源文本{report.distinct_echoes}种")
        if report.bot_responses:
            reasons.append(f"响应已知bot{report.bot_responses}次")
        report.reasons = tuple(reasons)
        return report

    def senders(self, now: float) -> list[dict[str, object]]:
        """列出窗口内出现过的发送者，用于排查"谁被当成了机器人"。

        互聊拦不住时，最常见的根因是对方账号根本没被识别为机器人，
        于是每条消息都被当成人类、把链条清零。这个方法把窗口内的
        发送者与各自的判定结果摊开，方便把账号补进名单。

        Args:
            now: 当前时间戳（秒）。

        Returns:
            list[dict]: 按最近发言排序的发送者统计。
        """

        self._prune(now)
        stats: dict[str, dict[str, object]] = {}
        for record in self._records:
            entry = stats.setdefault(
                record.uid,
                {
                    "uid": record.uid,
                    "count": 0,
                    "is_bot": False,
                    "last_ts": record.ts,
                    "preview": record.preview,
                    "platform": record.platform,
                    "nickname": record.nickname,
                    "cardname": record.cardname,
                    "identity_source": record.identity_source,
                },
            )
            entry["count"] = int(entry["count"]) + 1
            entry["is_bot"] = record.is_bot
            entry["platform"] = record.platform
            entry["nickname"] = record.nickname
            entry["cardname"] = record.cardname
            entry["identity_source"] = record.identity_source
            entry["preview"] = record.preview
            entry["last_ts"] = max(float(entry["last_ts"]), record.ts)
        return sorted(stats.values(), key=lambda item: float(item["last_ts"]), reverse=True)

    def _echo_source(self, candidate: MessageRecord) -> Optional[MessageRecord]:
        """找回声来源，双方均需足够长，避免短子串包含造成误判。"""

        minimum = max(_ECHO_MIN_LENGTH, self.params.min_text_length, self.params.identity_min_text_length)
        if len(candidate.normalized) < minimum:
            return None
        for record in reversed(self._records):
            if record.uid == candidate.uid or not record.uid:
                continue
            if not 0 <= candidate.ts - record.ts <= max(1.0, self.params.identity_response_within_seconds):
                continue
            if len(record.normalized) < minimum:
                continue
            ratio = min(len(record.normalized), len(candidate.normalized)) / max(len(record.normalized), len(candidate.normalized))
            if ratio < 0.65:
                continue
            if record.digest == candidate.digest:
                return record
            if bigram_containment(candidate.normalized, record.normalized) >= _ECHO_CONTAINMENT:
                return record
        return None

    def looks_like_bot_echo(self, *, now: float, uid: str, text: str) -> bool:
        """单次回声信号，仅供观测；身份提升由 identity_report 多轮证据决定。"""

        self._prune(now)
        return self._echo_source(self._make_record(now, uid, False, text)) is not None

    def consume_identity(self, uid: str) -> None:
        """提升身份时消耗证据；过期后须用新证据重新观察。"""

        self._suspect_strikes.pop(uid, None)
        self._last_suspect_ts.pop(uid, None)
        for item in self._records:
            if item.uid == uid:
                item.echo_source = ""
                item.echo_anchored = False
                item.responsive = False
                item.bot_response = False

    def forget_identity(self, uid: str) -> None:
        self.consume_identity(uid)
        for item in self._records:
            if item.uid == uid:
                item.is_bot = False

    def observe_human(self, now: float, uid: str = "") -> None:
        """记录一条人类消息：清零链条、衰减退避档位。"""

        self._last_human_ts = now
        self._chain_length = 0
        self._cooldown_until = 0.0
        if self.params.strike_decay_on_human and self._strike > 0:
            self._strike -= 1

    def evaluate(
        self,
        *,
        now: float,
        uid: str,
        is_bot: bool,
        text: str,
        is_self: bool = False,
        is_arbiter_primary: bool = True,
        platform: str = "",
        nickname: str = "",
        cardname: str = "",
        identity_source: str = "",
    ) -> Decision:
        """判定一条入站消息是否应当进入 MaiBot 主链。

        Args:
            now: 当前时间戳（秒）。
            uid: 发送者平台账号。
            is_bot: 发送者是否已被识别为机器人。
            text: 消息文本。
            is_self: 是否为本实例自己的账号（回声消息）。
            is_arbiter_primary: 多实例仲裁中本实例是否为主实例。

        Returns:
            Decision: 判定结果；`allow=False` 时调用方应中止消息链。
        """

        self._prune(now)
        record = self._make_record(now, uid, is_bot, text)
        record.platform = platform
        record.nickname = nickname
        record.cardname = cardname
        record.identity_source = identity_source

        if is_self:
            # 自己的消息回灌（适配器回声 / 多账号转发）直接丢，且不计入退避档位。
            self._suppressed += 1
            self._last_reason = "self_echo"
            return Decision(allow=False, reason="self_echo", chain_length=self._chain_length)

        previous = self._records[-1] if self._records else None
        if previous is not None and previous.uid and previous.uid != record.uid:
            delay = now - previous.ts
            record.responsive = 0 <= delay <= max(1.0, self.params.suspect_responsive_within_seconds)
            record.bot_response = record.responsive and previous.is_bot
        source = self._echo_source(record)
        if source is not None and previous is not None and previous.uid != record.uid:
            delay = now - previous.ts
            if 0 <= delay <= max(1.0, self.params.identity_response_within_seconds):
                record.echo_source = source.digest
                record.echo_anchored = source.is_bot and previous.uid == source.uid
        self._records.append(record)

        if not is_bot:
            self.observe_human(now, uid)
            return Decision(allow=True, reason="human")

        self._chain_length += 1
        remaining = self.cooldown_remaining(now)
        pool = [item for item in self._records if item.is_bot and item is not record and 0 <= now - item.ts <= self.params.window_seconds]
        duplicate = self._matches_any(record, pool)
        pingpong = self._pingpong(now)
        burst = self._burst(now)

        duplicate_threshold = max(1, self.params.duplicate_chain_threshold)
        max_chain = max(2, self.params.max_chain_length)
        if self.params.arbiter_enabled and not is_arbiter_primary:
            duplicate_threshold = self.params.arbiter_yield_threshold
            max_chain = max(2, self.params.max_chain_length - 1)

        reason = ""
        if remaining > 0:
            reason = "cooldown"
        elif self._chain_length >= max_chain:
            reason = "chain_limit"
        elif duplicate and self._chain_length >= duplicate_threshold:
            reason = "duplicate"
        elif pingpong and self._chain_length >= self.params.pingpong_chain_threshold:
            reason = "pingpong"
        elif burst and (duplicate or self._chain_length >= max(2, max_chain - 1)):
            reason = "burst"

        if not reason:
            self._allowed_bot += 1
            score = 0.0
            if duplicate:
                score += 0.5
            if pingpong:
                score += 0.3
            if burst:
                score += 0.2
            return Decision(
                allow=True,
                reason="allow_bot",
                duplicate=duplicate,
                pingpong=pingpong,
                burst=burst,
                chain_length=self._chain_length,
                score=round(score, 3),
            )

        duration = 0.0 if reason == "cooldown" else self._register_strike(now)
        self._suppressed += 1
        self._last_reason = reason
        learned = reason in {"duplicate", "pingpong"} and bool(duplicate or pingpong) and self._chain_length >= 2
        return Decision(
            allow=False,
            reason=reason,
            duplicate=duplicate,
            pingpong=pingpong,
            burst=burst,
            chain_length=self._chain_length,
            score=1.0,
            cooldown_seconds=duration,
            cooldown_remaining=self.cooldown_remaining(now),
            learned_bot=learned,
        )

    def check_outgoing(self, *, now: float, text: str, self_uid: str = "") -> Decision:
        """出站消息的最后一道闸门：与窗口内机器人消息／自己上一条回复查重。

        该检查在 `send_service.before_send` 调用，用于兜住"AI 已经把回复生成完"
        的情况——入站拦截再快，也拦不住已经在推理中的那一轮。
        放行时这条消息也会写入指纹窗口并推进链条：本实例自己的发言同样是这条
        对话里的一条机器人消息，不记账会让两只机器人的链条各算一半、永远到不了上限。
        """

        self._prune(now)
        record = self._make_record(now, self_uid, True, text)
        remaining = self.cooldown_remaining(now)
        next_chain = self._chain_length + 1
        max_chain = max(2, self.params.max_chain_length)
        if self.params.arbiter_enabled:
            max_chain = max(2, self.params.max_chain_length - 1)

        pool = [item for item in self._records if item.is_bot and 0 <= now - item.ts <= self.params.window_seconds]
        pool.extend(self._outgoing)
        duplicate = self._matches_any(record, pool)

        if remaining > 0:
            reason = "cooldown"
        elif next_chain >= max_chain:
            reason = "chain_limit"
        elif duplicate:
            reason = "duplicate_outgoing"
        else:
            reason = ""

        if not reason:
            self._records.append(record)
            self._chain_length = next_chain
            self._outgoing.append(record)
            return Decision(allow=True, reason="allow_outgoing", duplicate=False, chain_length=next_chain)

        self._suppressed += 1
        self._last_reason = reason
        if reason != "cooldown":
            self._register_strike(now)
        return Decision(
            allow=False,
            reason=reason,
            duplicate=duplicate,
            chain_length=self._chain_length,
            cooldown_seconds=self.params.cooldown_base_seconds if reason != "cooldown" else 0.0,
            cooldown_remaining=self.cooldown_remaining(now),
        )


class GuardRegistry:
    """按 stream_id 管理 `StreamGuard`，并按需回收长期不活跃的流。"""

    def __init__(self, params: Optional[GuardParams] = None, *, max_streams: int = 200) -> None:
        self.params = params or GuardParams()
        self.max_streams = max(8, max_streams)
        self._guards: dict[str, StreamGuard] = {}
        self._touched: dict[str, float] = {}

    @staticmethod
    def _scope_parts(key: str) -> tuple[str, str]:
        try:
            parts = json.loads(key)
        except (TypeError, ValueError):
            return "", key
        if isinstance(parts, list) and len(parts) == 2 and all(isinstance(item, str) for item in parts):
            return parts[0], parts[1]
        return "", key

    def _matching_keys(self, stream_id: str) -> list[str]:
        return [key for key in self._guards
                if key == stream_id or self._scope_parts(key)[1] == stream_id
                or ":".join(self._scope_parts(key)) == stream_id]

    def get(self, stream_id: str, now: float, *, platform: str = "") -> StreamGuard:
        """平台与聊天流共同隔离证据及冷却。无平台的查询只允许唯一匹配。"""

        key = json.dumps([platform.lower(), stream_id], separators=(",", ":")) if platform else stream_id
        if not platform and key not in self._guards:
            matches = self._matching_keys(stream_id)
            if len(matches) > 1:
                raise ValueError("同名聊天流存在多个平台，请使用 平台:stream_id 查询")
            if matches:
                key = matches[0]
        guard = self._guards.get(key)
        if guard is None:
            guard = StreamGuard(self.params)
            self._guards[key] = guard
        guard.platform, guard.stream_id = self._scope_parts(key)
        self._touched[key] = now
        if len(self._guards) > self.max_streams:
            self._evict(now)
        return guard

    def _evict(self, now: float) -> None:
        """按最后活跃时间淘汰最久未使用的流。"""

        stale = sorted(self._touched.items(), key=lambda item: item[1])
        for stream_id, _ in stale[: max(1, len(stale) - self.max_streams)]:
            self._guards.pop(stream_id, None)
            self._touched.pop(stream_id, None)

    def snapshot(self, now: float) -> dict[str, dict[str, object]]:
        """导出全部流的状态快照。"""

        return {
            stream_id: guard.snapshot(now)
            for stream_id, guard in self._guards.items()
            if guard.suppressed or guard.chain_length or guard.cooldown_remaining(now) > 0
        }

    def restore(self, data: dict[str, dict[str, object]], now: float) -> None:
        """按快照恢复流状态。"""

        for stream_id, payload in (data or {}).items():
            if not isinstance(payload, dict):
                continue
            guard = StreamGuard(self.params)
            guard.restore(payload, now)
            guard.platform, guard.stream_id = self._scope_parts(str(stream_id))
            self._guards[str(stream_id)] = guard
            self._touched[str(stream_id)] = now

    def forget_identity(self, uid: str, *, platform: str = "") -> None:
        for guard in self._guards.values():
            if not platform or guard.platform == platform.lower():
                guard.forget_identity(uid)

    def reset(self, stream_id: str = "") -> list[str]:
        """重置指定流（留空则全部）并返回被重置的流 ID 列表。"""

        if stream_id:
            keys = self._matching_keys(stream_id)
            for key in keys:
                self._guards.pop(key, None)
                self._touched.pop(key, None)
            return keys
        cleared = list(self._guards)
        self._guards.clear()
        self._touched.clear()
        return cleared
