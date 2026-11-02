"""群聊复读守卫（Group Loop Guard）—— MaiBot 插件入口。

解决的问题
----------
同一个 QQ 群里放了不止一个 AI 机器人时，机器人之间会互相"接话"：
A 的回复触发 B 的回复，B 的回复又触发 A，几轮之后两边开始说出高度相似
甚至逐字重复的内容，既烧 token 又把群聊刷成复读现场。

本插件在 MaiBot 插件系统的三个位置下闸：

1. ``chat.receive.before_process``（入站，可 abort）
   —— 在消息进入 ``SessionMessage.process()`` 之前判定，拦下的消息不会写库、
   不会进 HeartFlow / Maisaka 上下文，也不会触发推理，省 token 且不污染记忆。
2. ``send_service.before_send``（出站，可 abort）
   —— 兜住"回复已经生成完、正在往外发"的那一轮，按同一份指纹库查重。
3. ``maisaka.planner.before_request``（可选，软静音）
   —— 冷却期内从工具定义里摘掉 reply 工具，让规划器这一轮没有"开口"的手段。

判定逻辑与阈值全部在 ``loopguard_core.py``，本文件只负责身份识别、配置映射、
状态持久化与命令。

安装位置：``MaiBot/plugins/maibot-group-loopguard/``
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, ClassVar

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder


def _load_core_module() -> Any:
    """加载核心算法模块。

    Runner 以 ``submodule_search_locations`` 的方式加载插件入口，因此正常情况下
    走相对导入；某些自定义加载器只把 ``plugin.py`` 当单文件执行，此时退化为按
    路径加载同目录的 ``loopguard_core.py``。
    """

    try:  # 常规路径：插件目录被当作包加载
        from . import loopguard_core as core_module

        return core_module
    except Exception:
        import importlib.util
        import sys

        core_path = Path(__file__).with_name("loopguard_core.py")
        spec = importlib.util.spec_from_file_location("maibot_loopguard_core", core_path)
        if spec is None or spec.loader is None:  # pragma: no cover - 文件缺失时才会命中
            raise ImportError(f"未能加载 {core_path}") from None
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("maibot_loopguard_core", module)
        spec.loader.exec_module(module)
        return module


_CORE = _load_core_module()
GuardParams = _CORE.GuardParams
GuardRegistry = _CORE.GuardRegistry
normalize_text = _CORE.normalize_text
hamming_distance = _CORE.hamming_distance

_STATE_FILE = "loopguard_state.json"
_STATE_VERSION = 1


# ─── 配置模型 ────────────────────────────────────────────────────────


class PluginSection(PluginConfigBase):
    """插件总开关。"""

    __ui_label__ = "插件"
    __ui_icon__ = "shield"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用复读守卫")
    config_version: str = Field(default="1.0.0", description="配置版本，勿手动改")
    dry_run: bool = Field(default=False, description="演习模式：只记日志、照常放行，用于上线前观察误杀")


class IdentitySection(PluginConfigBase):
    """机器人身份识别。"""

    __ui_label__ = "身份识别"
    __ui_icon__ = "users"
    __ui_order__ = 1

    bot_accounts: list[str] = Field(
        default_factory=list,
        description="已知机器人的平台账号，建议把同群其它 AI 的 QQ 号都填进来；可写 123456 或 qq:123456",
    )
    self_accounts: list[str] = Field(
        default_factory=list,
        description="本实例自己的账号；留空时从 bot_config.toml 的 bot.qq_account / bot.platforms 自动读取",
    )
    name_patterns: list[str] = Field(
        default_factory=lambda: [r"(?i)(bot|机器人|助手|麦麦|maisaka|assistant)$"],
        description="昵称/群名片正则，命中即按机器人处理；写 .* 等于关闭昵称识别",
    )
    auto_learn_behavior: bool = Field(
        default=True,
        description="行为学习：未登记账号若在窗口内复述其它机器人的话，自动记为机器人",
    )
    learned_ttl_seconds: int = Field(default=86400, description="行为学习结果的保留时长（秒），到期自动遗忘")
    platform_scope: list[str] = Field(
        default_factory=lambda: ["qq"],
        description="生效平台；留空表示所有平台。非名单内平台的任何消息都原样放行",
    )


class DetectSection(PluginConfigBase):
    """复读检测阈值。"""

    __ui_label__ = "检测阈值"
    __ui_icon__ = "scan"
    __ui_order__ = 2

    window_seconds: int = Field(default=180, description="滑动窗口长度（秒），窗口外的消息不参与查重")
    max_records: int = Field(default=60, description="每个聊天流最多保留多少条指纹")
    min_text_length: int = Field(default=4, description="短于该长度的文本不参与查重，避免把'哈哈'判成复读")
    simhash_distance: int = Field(
        default=1, description="SimHash 汉明距离阈值；1 等价于只判定逐字重复，调大会引入噪声，建议不超过 6"
    )
    bigram_similarity_threshold: float = Field(default=0.72, description="短文本 2-gram Jaccard 相似度阈值")
    bigram_containment_threshold: float = Field(
        default=0.85, description="2-gram 包含度阈值，专门抓'抄一遍再补几个字'的复读"
    )
    max_chain_length: int = Field(default=4, description="自最近一条人类消息起，连续多少条机器人消息后强制静音")
    duplicate_chain_threshold: int = Field(default=2, description="连续第几条机器人消息起，内容重复就拦")
    pingpong_span: int = Field(default=6, description="检测'两个机器人互相接话'时回看的消息条数")
    pingpong_chain_threshold: int = Field(default=3, description="交替节奏成立时的最小连续条数")
    burst_count: int = Field(default=6, description="爆发检测：窗口内机器人消息达到该条数算爆发")
    burst_window_seconds: int = Field(default=60, description="爆发检测的时间窗（秒）")


class ActionSection(PluginConfigBase):
    """处置策略。"""

    __ui_label__ = "处置"
    __ui_icon__ = "octagon"
    __ui_order__ = 3

    block_incoming: bool = Field(default=True, description="拦截入站机器人消息（主刹车）")
    block_self_echo: bool = Field(default=True, description="拦截本实例自己账号的回声消息")
    block_outgoing: bool = Field(default=True, description="在发送前再查一次重（最后一道闸）")
    cooldown_base_seconds: int = Field(default=120, description="首次触发后的冷却时长（秒）")
    cooldown_multiplier: float = Field(default=2.0, description="每次再触发的冷却倍率")
    cooldown_max_seconds: int = Field(default=1800, description="冷却时长上限（秒）")
    human_reset_seconds: int = Field(default=300, description="留档：人类发言后多久内视为活跃（当前实现为立即清零链条）")
    frequency_brake: bool = Field(
        default=False,
        description="冷却期内把该聊天流的回复频率倍率压到 brake_value（依赖 frequency.set_adjust 能力）",
    )
    frequency_brake_value: float = Field(default=0.0, description="冷却期内的频率倍率，0 表示完全不出声")
    frequency_restore_value: float = Field(default=1.0, description="解除冷却时恢复的频率倍率")
    soft_mute_reply_tool: bool = Field(
        default=False,
        description="冷却期内从 Maisaka 规划器的工具定义里摘掉 reply 工具，从源头减少一轮推理",
    )
    muted_tool_names: list[str] = Field(default_factory=lambda: ["reply"], description="软静音时要摘掉的工具名")
    notify_on_trip: bool = Field(default=False, description="触发时在当前群发一条提示（默认关闭，避免自己刷屏）")
    notify_text: str = Field(default="（复读守卫已介入，暂停接话）", description="提示文案")


class ArbiterSection(PluginConfigBase):
    """多实例仲裁。"""

    __ui_label__ = "多实例仲裁"
    __ui_icon__ = "scale"
    __ui_order__ = 4

    enabled: bool = Field(default=False, description="启用账号优先级仲裁，让同群里多个实例的发言权错开")
    peers: list[str] = Field(default_factory=list, description="同群其它实例的账号列表，用于计算优先级")
    self_priority: str = Field(default="", description="本实例对外声明的账号；留空时取自动识别的自身账号")


class CommandSection(PluginConfigBase):
    """命令权限。"""

    __ui_label__ = "命令"
    __ui_icon__ = "terminal"
    __ui_order__ = 5

    command_operators: list[str] = Field(
        default_factory=list, description="允许使用 /loopguard 的 QQ 号；控制台与 WebUI 始终放行"
    )


class LoopGuardConfig(PluginConfigBase):
    """复读守卫总配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    identity: IdentitySection = Field(default_factory=IdentitySection)
    detect: DetectSection = Field(default_factory=DetectSection)
    action: ActionSection = Field(default_factory=ActionSection)
    arbiter: ArbiterSection = Field(default_factory=ArbiterSection)
    command: CommandSection = Field(default_factory=CommandSection)


# ─── 消息解析工具 ────────────────────────────────────────────────────


def _extract_text(message: dict[str, Any]) -> str:
    """从 Hook 载荷里取可读文本。

    入站 Hook 触发时 ``processed_plain_text`` 还没有生成，只能从
    ``raw_message`` 的组件列表里拼接；出站消息则优先用已处理好的纯文本。
    """

    plain = message.get("processed_plain_text")
    if isinstance(plain, str) and plain.strip():
        return plain

    parts: list[str] = []
    raw = message.get("raw_message")
    if isinstance(raw, list):
        for component in raw:
            if not isinstance(component, dict):
                continue
            component_type = str(component.get("type") or "")
            data = component.get("data")
            if component_type == "text" and isinstance(data, str):
                parts.append(data)
            elif component_type == "at" and isinstance(data, dict):
                nickname = data.get("target_user_nickname") or data.get("target_user_cardname") or ""
                parts.append(f"@{nickname}")
            elif component_type == "reply" and isinstance(data, dict):
                quoted = data.get("target_message_content")
                if isinstance(quoted, str) and quoted.strip():
                    parts.append(quoted)
    return "".join(parts)


def _extract_at_targets(message: dict[str, Any]) -> set[str]:
    """取出消息里被 @ 的账号集合。"""

    targets: set[str] = set()
    raw = message.get("raw_message")
    if not isinstance(raw, list):
        return targets
    for component in raw:
        if not isinstance(component, dict) or component.get("type") != "at":
            continue
        data = component.get("data")
        if isinstance(data, dict):
            target = str(data.get("target_user_id") or "").strip()
            if target:
                targets.add(target)
    return targets


def _norm_account(raw: str) -> str:
    """把 ``platform:account`` 或裸账号统一成账号部分。"""

    text = str(raw or "").strip()
    if ":" in text:
        return text.split(":", 1)[1].strip()
    return text


def _norm_platform_account(raw: str) -> tuple[str, str]:
    """拆出 ``(platform, account)``，无平台前缀时平台留空。"""

    text = str(raw or "").strip()
    if ":" in text:
        platform, account = text.split(":", 1)
        return platform.strip().lower(), account.strip()
    return "", text


# ─── 插件主体 ────────────────────────────────────────────────────────


class LoopGuardPlugin(MaiBotPlugin):
    """多机器人同群时的复读守卫。"""

    config_model = LoopGuardConfig
    config_reload_subscriptions: ClassVar[tuple[str, ...]] = ("bot",)

    def __init__(self) -> None:
        """初始化内存状态；真正的配置解析放到 ``on_load``。"""

        super().__init__()
        self._registry: Any = None
        self._self_accounts: dict[str, set[str]] = {}
        self._learned: dict[str, float] = {}
        self._braked: set[str] = set()
        self._state_path: Path | None = None
        self._save_task: asyncio.Task[None] | None = None
        self._dirty = False
        self._notice_depth = 0
        self._stats: dict[str, int] = {"incoming_blocked": 0, "outgoing_blocked": 0, "allowed_bot": 0}

    # ── 生命周期 ────────────────────────────────────────────────

    async def on_load(self) -> None:
        """构建状态机、恢复持久化状态并启动后台落盘。"""

        self._registry = GuardRegistry(self._build_params(), max_streams=200)
        self._state_path = Path(self.ctx.paths.data_dir) / _STATE_FILE
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        await self._resolve_self_accounts()
        self._load_state()
        self._save_task = asyncio.create_task(self._save_loop())
        self.ctx.logger.info(
            "复读守卫已加载：enabled=%s dry_run=%s self=%s",
            self.config.plugin.enabled,
            self.config.plugin.dry_run,
            sorted(account for accounts in self._self_accounts.values() for account in accounts) or "未识别",
        )

    async def on_unload(self) -> None:
        """停掉后台任务并落盘。"""

        if self._save_task is not None:
            self._save_task.cancel()
            try:
                await self._save_task
            except asyncio.CancelledError:
                pass
            finally:
                self._save_task = None
        await self._flush_state()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """配置热重载：重建阈值、重新识别自身账号。"""

        self.ctx.logger.info("复读守卫配置更新：scope=%s version=%s", scope, version)
        self._registry = GuardRegistry(self._build_params(), max_streams=200)
        await self._resolve_self_accounts()
        self._dirty = True

    # ── 配置映射 ────────────────────────────────────────────────

    def _build_params(self) -> Any:
        """把插件配置映射成核心算法的参数对象。"""

        detect = self.config.detect
        action = self.config.action
        arbiter = self.config.arbiter
        return GuardParams(
            window_seconds=float(detect.window_seconds),
            max_records=int(detect.max_records),
            min_text_length=int(detect.min_text_length),
            simhash_distance=int(detect.simhash_distance),
            bigram_similarity_threshold=float(detect.bigram_similarity_threshold),
            bigram_containment_threshold=float(detect.bigram_containment_threshold),
            max_chain_length=int(detect.max_chain_length),
            duplicate_chain_threshold=int(detect.duplicate_chain_threshold),
            pingpong_span=int(detect.pingpong_span),
            pingpong_chain_threshold=int(detect.pingpong_chain_threshold),
            burst_count=int(detect.burst_count),
            burst_window_seconds=float(detect.burst_window_seconds),
            cooldown_base_seconds=float(action.cooldown_base_seconds),
            cooldown_multiplier=float(action.cooldown_multiplier),
            cooldown_max_seconds=float(action.cooldown_max_seconds),
            human_reset_seconds=float(action.human_reset_seconds),
            arbiter_enabled=bool(arbiter.enabled),
            arbiter_yield_threshold=1,
        )

    # ── 身份识别 ────────────────────────────────────────────────

    async def _resolve_self_accounts(self) -> None:
        """读取全局配置里登记的自身账号，并合并用户手填项。"""

        accounts: dict[str, set[str]] = {}

        def _add(platform: str, account: str) -> None:
            account = _norm_account(account)
            if not account or account == "0":
                return
            accounts.setdefault((platform or "").lower(), set()).add(account)

        try:
            primary_platform = str(await self.ctx.config.get("bot.platform", "qq") or "qq").lower()
            primary_account = str(await self.ctx.config.get("bot.qq_account", "") or "")
            if primary_account:
                _add(primary_platform, primary_account)

            platforms = await self.ctx.config.get("bot.platforms", []) or []
            if isinstance(platforms, list):
                for entry in platforms:
                    platform, account = _norm_platform_account(str(entry))
                    if account:
                        _add(platform or primary_platform, account)
        except Exception as exc:  # 全局配置读失败不影响主流程
            self.ctx.logger.warning("读取全局 Bot 配置失败，改用插件内 self_accounts：%s", exc)

        for entry in self.config.identity.self_accounts:
            platform, account = _norm_platform_account(entry)
            if account:
                _add(platform, account)

        self._self_accounts = accounts

    def _is_self(self, platform: str, user_id: str) -> bool:
        """判断账号是否属于本实例。"""

        account = _norm_account(user_id)
        if not account:
            return False
        platform_key = str(platform or "").lower()
        for key, accounts in self._self_accounts.items():
            if key and platform_key and key != platform_key:
                continue
            if account in accounts:
                return True
        return False

    def _primary_self_id(self, platform: str) -> str:
        """取当前平台下的自身账号，用于出站消息落指纹。"""

        platform_key = str(platform or "").lower()
        accounts = self._self_accounts.get(platform_key) or next(
            (value for key, value in self._self_accounts.items() if not key), set()
        )
        return sorted(accounts)[0] if accounts else "self"

    def _learned_hit(self, user_id: str, now: float) -> bool:
        """判断账号是否在行为学习名单内且未过期。"""

        expire_at = self._learned.get(user_id)
        if expire_at is None:
            return False
        if expire_at <= now:
            self._learned.pop(user_id, None)
            self._dirty = True
            return False
        return True

    def _remember_learned(self, user_id: str, now: float) -> None:
        """把一个账号写入行为学习名单。"""

        if not user_id:
            return
        ttl = max(60, int(self.config.identity.learned_ttl_seconds))
        self._learned[user_id] = now + ttl
        self._dirty = True

    def _is_bot_account(self, platform: str, user_id: str, nickname: str, cardname: str, now: float) -> bool:
        """按"名单 → 行为学习 → 昵称正则"的顺序判定发送者是不是机器人。"""

        account = _norm_account(user_id)
        for entry in self.config.identity.bot_accounts:
            entry_platform, entry_account = _norm_platform_account(entry)
            if not entry_account or entry_account != account:
                continue
            if not entry_platform or entry_platform == str(platform or "").lower():
                return True
        if self._learned_hit(account, now):
            return True
        for pattern in self.config.identity.name_patterns:
            try:
                if re.search(pattern, f"{nickname} {cardname}".strip()):
                    return True
            except re.error:
                self.ctx.logger.warning("昵称正则无效已跳过：%s", pattern)
        return False

    def _platform_allowed(self, platform: str) -> bool:
        """判断当前平台是否在生效范围内。"""

        scope = [str(item).lower() for item in self.config.identity.platform_scope if str(item).strip()]
        if not scope:
            return True
        return str(platform or "").lower() in scope

    def _arbiter_primary(self) -> bool:
        """多实例仲裁：账号字典序最小的那个实例承担主要发言权。"""

        arbiter = self.config.arbiter
        if not arbiter.enabled:
            return True
        own = _norm_account(arbiter.self_priority) or self._primary_self_id("")
        candidates = {_norm_account(item) for item in arbiter.peers if _norm_account(item)}
        if own:
            candidates.add(own)
        if not candidates:
            return True
        return own == sorted(candidates)[0]

    # ── 冷却联动 ────────────────────────────────────────────────

    async def _engage_brake(self, stream_id: str) -> None:
        """冷却期内压低回复频率（可选能力）。"""

        if not self.config.action.frequency_brake or stream_id in self._braked:
            return
        try:
            await self.ctx.frequency.set_adjust(stream_id, float(self.config.action.frequency_brake_value))
            self._braked.add(stream_id)
        except Exception as exc:
            self.ctx.logger.debug("频率刹车未生效：%s", exc)

    async def _release_brake(self, stream_id: str, now: float) -> None:
        """冷却结束后恢复回复频率。"""

        if stream_id not in self._braked or self._registry is None:
            return
        guard = self._registry.get(stream_id, now)
        if guard.cooldown_remaining(now) > 0:
            return
        try:
            await self.ctx.frequency.set_adjust(stream_id, float(self.config.action.frequency_restore_value))
        except Exception as exc:
            self.ctx.logger.debug("频率恢复未生效：%s", exc)
        self._braked.discard(stream_id)

    async def _maybe_notify(self, stream_id: str) -> None:
        """按配置在群里发一条提示，避免把自己也拦掉。"""

        if not self.config.action.notify_on_trip or not stream_id:
            return
        self._notice_depth += 1
        try:
            await self.ctx.send.text(self.config.action.notify_text, stream_id)
        except Exception as exc:
            self.ctx.logger.debug("提示发送失败：%s", exc)
        finally:
            self._notice_depth -= 1

    # ── Hook：入站 ──────────────────────────────────────────────

    @HookHandler(
        "chat.receive.before_process",
        name="loopguard_incoming",
        description="多机器人同群时拦截复读消息，避免互相触发无限对话",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def guard_incoming(self, **kwargs: Any) -> dict[str, Any] | None:
        """入站闸门：判完再决定是否让消息进入 MaiBot 主链。"""

        if self._registry is None or not self.config.plugin.enabled:
            return None
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return None

        platform = str(message.get("platform") or "")
        if not self._platform_allowed(platform):
            return None

        stream_id = str(message.get("session_id") or message.get("message_id") or "")
        if not stream_id:
            return None

        info = message.get("message_info") or {}
        user = info.get("user_info") if isinstance(info, dict) else None
        user = user if isinstance(user, dict) else {}
        user_id = str(user.get("user_id") or "")
        nickname = str(user.get("user_nickname") or "")
        cardname = str(user.get("user_cardname") or "")

        now = time.time()
        text = _extract_text(message)
        guard = self._registry.get(stream_id, now)

        is_self = self._is_self(platform, user_id)
        is_bot = is_self or self._is_bot_account(platform, user_id, nickname, cardname, now)

        if not is_bot and self.config.identity.auto_learn_behavior:
            try:
                if guard.looks_like_bot_echo(now=now, uid=user_id, text=text):
                    is_bot = True
                    self._remember_learned(_norm_account(user_id), now)
                    self.ctx.logger.info(
                        "[loopguard] 行为识别：%s(%s) 复述了机器人内容，已临时记为机器人", nickname or user_id, user_id
                    )
            except Exception as exc:
                self.ctx.logger.debug("行为识别失败已跳过：%s", exc)

        # 机器人 @ 另一个已知机器人：这是最典型的互激链路，直接按机器人处理。
        at_targets = _extract_at_targets(message)
        if not is_bot and at_targets:
            known = {_norm_account(item) for item in self.config.identity.bot_accounts if _norm_account(item)}
            known |= set(self._self_accounts.get(str(platform).lower(), set()))
            if at_targets & known:
                is_bot = True

        decision = guard.evaluate(
            now=now,
            uid=user_id,
            is_bot=is_bot,
            text=text,
            is_self=is_self and self.config.action.block_self_echo,
            is_arbiter_primary=self._arbiter_primary(),
        )

        if decision.allow:
            if is_bot:
                self._stats["allowed_bot"] += 1
            await self._release_brake(stream_id, now)
            return None

        if not self.config.action.block_incoming:
            self.ctx.logger.debug(
                "[loopguard] 入站拦截已关闭，本条放行 stream=%s reason=%s", stream_id, decision.reason
            )
            return None

        if self.config.plugin.dry_run:
            self.ctx.logger.warning(
                "[loopguard][dry-run] 本应拦截 stream=%s sender=%s reason=%s chain=%s cooldown=%.0fs text=%r",
                stream_id,
                user_id,
                decision.reason,
                decision.chain_length,
                decision.cooldown_remaining,
                text[:60],
            )
            return None

        self._stats["incoming_blocked"] += 1
        self._dirty = True
        self.ctx.logger.info(
            "[loopguard] 拦截入站 stream=%s sender=%s reason=%s chain=%s cooldown=%.0fs text=%r",
            stream_id,
            user_id,
            decision.reason,
            decision.chain_length,
            decision.cooldown_remaining,
            text[:60],
        )
        await self._engage_brake(stream_id)
        await self._maybe_notify(stream_id)
        return {"action": "abort"}

    # ── Hook：出站 ──────────────────────────────────────────────

    @HookHandler(
        "send_service.before_send",
        name="loopguard_outgoing",
        description="发送前对回复正文再做一次查重，兜住已经生成完的那一轮",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def guard_outgoing(self, **kwargs: Any) -> dict[str, Any] | None:
        """出站闸门：冷却期内或内容与外发历史重复时取消发送。"""

        if self._registry is None or not self.config.plugin.enabled:
            return None
        if not self.config.action.block_outgoing or self._notice_depth > 0:
            return None

        message = kwargs.get("message")
        if not isinstance(message, dict):
            return None

        platform = str(message.get("platform") or "")
        if not self._platform_allowed(platform):
            return None

        stream_id = str(message.get("session_id") or "")
        if not stream_id:
            return None

        text = _extract_text(message)
        if not normalize_text(text):
            return None

        now = time.time()
        guard = self._registry.get(stream_id, now)
        decision = guard.check_outgoing(now=now, text=text, self_uid=self._primary_self_id(platform))
        if decision.allow:
            return None

        if self.config.plugin.dry_run:
            self.ctx.logger.warning(
                "[loopguard][dry-run] 本应取消发送 stream=%s reason=%s text=%r",
                stream_id,
                decision.reason,
                text[:60],
            )
            return None

        self._stats["outgoing_blocked"] += 1
        self._dirty = True
        self.ctx.logger.info(
            "[loopguard] 取消发送 stream=%s reason=%s cooldown=%.0fs text=%r",
            stream_id,
            decision.reason,
            decision.cooldown_remaining,
            text[:60],
        )
        return {"action": "abort"}

    # ── Hook：软静音（可选）─────────────────────────────────────

    @HookHandler(
        "maisaka.planner.before_request",
        name="loopguard_soft_mute",
        description="冷却期内从规划器工具定义里摘掉 reply，减少无意义的一轮推理",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def soft_mute_planner(self, **kwargs: Any) -> dict[str, Any] | None:
        """冷却期内移除 reply 类工具。"""

        if self._registry is None or not self.config.plugin.enabled:
            return None
        if not self.config.action.soft_mute_reply_tool:
            return None

        stream_id = str(kwargs.get("session_id") or "")
        if not stream_id:
            return None

        now = time.time()
        guard = self._registry.get(stream_id, now)
        if guard.cooldown_remaining(now) <= 0:
            return None

        muted = {str(name) for name in self.config.action.muted_tool_names if str(name).strip()}
        definitions = kwargs.get("tool_definitions")
        if not isinstance(definitions, list) or not definitions:
            return None

        kept = []
        for definition in definitions:
            if not isinstance(definition, dict):
                continue
            function = definition.get("function")
            name = str(function.get("name") or "") if isinstance(function, dict) else str(definition.get("name") or "")
            if name and name in muted:
                continue
            kept.append(definition)

        if len(kept) == len(definitions):
            return None

        kwargs["tool_definitions"] = kept
        self.ctx.logger.info("[loopguard] 冷却期内软静音，已移除工具 %s", sorted(muted))
        return {"action": "continue", "modified_kwargs": kwargs}

    # ── 命令 ────────────────────────────────────────────────────

    @Command(
        "loopguard",
        description="查看/重置复读守卫状态",
        pattern=r"^/loopguard(?:\s+(?P<sub>status|reset|learn|forget|on|off))?(?:\s+(?P<arg>\S+))?\s*$",
    )
    async def handle_command(self, **kwargs: Any) -> tuple[bool, str, bool]:
        """处理 ``/loopguard`` 命令族。"""

        if not self._can_command(kwargs):
            return False, "该命令仅限管理员使用", True

        matched = kwargs.get("matched_groups") or {}
        sub = str(matched.get("sub") or "status").lower()
        arg = str(matched.get("arg") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "")
        now = time.time()

        if sub == "on":
            self.config.plugin.enabled = True
            return True, "复读守卫已启用（本进程内生效）", True
        if sub == "off":
            self.config.plugin.enabled = False
            return True, "复读守卫已暂停（本进程内生效）", True
        if sub == "reset":
            cleared = self._registry.reset(arg) if self._registry is not None else []
            return True, f"已重置 {len(cleared)} 个聊天流状态", True
        if sub == "learn":
            if not arg:
                return False, "用法：/loopguard learn <账号>", True
            self._remember_learned(_norm_account(arg), now)
            return True, f"已把 {_norm_account(arg)} 记为机器人，{self.config.identity.learned_ttl_seconds} 秒后遗忘", True
        if sub == "forget":
            if not arg:
                return False, "用法：/loopguard forget <账号>", True
            self._learned.pop(_norm_account(arg), None)
            self._dirty = True
            return True, f"已移除行为学习记录：{_norm_account(arg)}", True

        if self._registry is None:
            return False, "守卫尚未初始化", True
        target = arg or stream_id
        guard = self._registry.get(target, now) if target else None
        if guard is None:
            return False, "未找到指定的聊天流", True
        snap = guard.snapshot(now)
        report = (
            f"流 {target}：连续机器人消息 {snap['chain_length']} 条，退避档位 {snap['strike']}，"
            f"剩余冷却 {snap['cooldown_remaining']}s，累计拦截 {snap['suppressed']} 次，"
            f"最近原因 {snap['last_reason'] or '无'}；窗口指纹 {snap['window_records']} 条；"
            f"全局统计 入站拦截 {self._stats['incoming_blocked']}、出站拦截 {self._stats['outgoing_blocked']}"
        )
        return True, report, True

    def _can_command(self, kwargs: dict[str, Any]) -> bool:
        """命令权限：控制台/WebUI 或配置里的操作员。"""

        if bool(kwargs.get("is_local_operator")):
            return True
        user_id = _norm_account(str(kwargs.get("user_id") or ""))
        operators = {_norm_account(item) for item in self.config.command.command_operators if _norm_account(item)}
        return bool(user_id and user_id in operators)

    # ── 状态持久化 ──────────────────────────────────────────────

    def _load_state(self) -> None:
        """读取上一次运行留下的学习名单与冷却状态。"""

        if self._state_path is None or not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.ctx.logger.warning("状态文件损坏，已忽略：%s", exc)
            return
        if not isinstance(payload, dict):
            return

        now = time.time()
        learned = payload.get("learned")
        if isinstance(learned, dict):
            for key, value in learned.items():
                try:
                    expire_at = float(value)
                except (TypeError, ValueError):
                    continue
                if expire_at > now:
                    self._learned[str(key)] = expire_at
        streams = payload.get("streams")
        if isinstance(streams, dict) and self._registry is not None:
            self._registry.restore({str(k): v for k, v in streams.items() if isinstance(v, dict)}, now)
        stats = payload.get("stats")
        if isinstance(stats, dict):
            for key in self._stats:
                try:
                    self._stats[key] = int(stats.get(key, 0))
                except (TypeError, ValueError):
                    continue

    async def _flush_state(self) -> None:
        """把内存状态写入磁盘（原子替换）。"""

        if self._state_path is None or self._registry is None:
            return
        now = time.time()
        payload = {
            "version": _STATE_VERSION,
            "saved_at": now,
            "learned": {key: value for key, value in self._learned.items() if value > now},
            "streams": self._registry.snapshot(now),
            "stats": dict(self._stats),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)

        def _write() -> None:
            assert self._state_path is not None
            tmp_path = self._state_path.with_suffix(".tmp")
            tmp_path.write_text(text, encoding="utf-8")
            tmp_path.replace(self._state_path)

        try:
            await asyncio.to_thread(_write)
            self._dirty = False
        except Exception as exc:
            self.ctx.logger.warning("状态落盘失败：%s", exc)

    async def _save_loop(self) -> None:
        """后台定时落盘，避免每条消息都写文件。"""

        while True:
            try:
                await asyncio.sleep(30)
                if self._dirty:
                    await self._flush_state()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - 兜底
                self.ctx.logger.debug("后台落盘异常：%s", exc)


def create_plugin() -> LoopGuardPlugin:
    """返回插件实例。"""

    return LoopGuardPlugin()
