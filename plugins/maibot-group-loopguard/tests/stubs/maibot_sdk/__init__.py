"""`maibot_sdk` 的测试替身。

只实现本插件用到的表面：基类、配置字段、三个组件装饰器、枚举。
真实的 SDK 由 Host 的 Runner 提供；这里的目的只是在没有 MaiBot 环境时也能
把 `plugin.py` 的 Hook 逻辑跑起来。
"""

from __future__ import annotations

from typing import Any, Callable

from . import types


def Field(default: Any = None, *, default_factory: Callable[[], Any] | None = None, description: str = "") -> Any:  # noqa: N802
    """返回字段默认值；`description` 仅为对齐真实 SDK 的签名。"""

    del description
    return default_factory() if default_factory is not None else default


def _component_decorator(*args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """组件装饰器替身：原样返回被装饰函数。"""

    del args, kwargs

    def _wrap(func: Callable[..., Any]) -> Callable[..., Any]:
        return func

    return _wrap


HookHandler = _component_decorator
Command = _component_decorator
Tool = _component_decorator
EventHandler = _component_decorator
API = _component_decorator


class PluginConfigBase:
    """配置模型替身：把类属性上的默认值复制到实例，避免测试之间互相污染。"""

    def __init__(self, **kwargs: Any) -> None:
        for name, value in vars(type(self)).items():
            if name.startswith("_"):
                continue
            if isinstance(value, PluginConfigBase):
                self.__dict__[name] = type(value)()
            elif isinstance(value, list):
                self.__dict__[name] = list(value)
            elif isinstance(value, dict):
                self.__dict__[name] = dict(value)
            else:
                self.__dict__[name] = value
        self.__dict__.update(kwargs)


class MaiBotPlugin:
    """插件基类替身。"""

    config_model: Any = None

    def __init__(self) -> None:
        self._ctx: Any = None
        self._plugin_config_instance: Any = None

    @property
    def config(self) -> Any:
        if self._plugin_config_instance is None:
            if self.config_model is None:
                raise RuntimeError("未声明 config_model")
            self._plugin_config_instance = self.config_model()
        return self._plugin_config_instance

    @property
    def ctx(self) -> Any:
        if self._ctx is None:
            raise RuntimeError("插件上下文尚未初始化")
        return self._ctx

    def _set_context(self, ctx: Any) -> None:
        self._ctx = ctx


__all__ = [
    "API",
    "Command",
    "EventHandler",
    "Field",
    "HookHandler",
    "MaiBotPlugin",
    "PluginConfigBase",
    "Tool",
    "types",
]
