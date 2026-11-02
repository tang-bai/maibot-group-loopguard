"""`maibot_sdk.types` 的测试替身。"""

from __future__ import annotations

from enum import Enum


class HookMode(str, Enum):
    BLOCKING = "blocking"
    OBSERVE = "observe"


class HookOrder(str, Enum):
    EARLY = "early"
    NORMAL = "normal"
    LATE = "late"


class ErrorPolicy(str, Enum):
    ABORT = "abort"
    SKIP = "skip"
    LOG = "log"
