"""神经闭环反馈 (maibot-corpus-callosum) 插件。

给 replyer 一条内部否决通道（"左右脑内部链接"）：

- 在 ``maisaka.replyer.before_request`` 注入"拒绝协议"提示词，告知 replyer
  当规划器指令明显有误时可只输出 ``<reject>理由</reject>`` 哨兵标记；
- 在 ``maisaka.replyer.after_response`` 拦截哨兵：把 ``response`` 置空使
  reply 工具静默失败（不向群里发送任何内容、不中止思考循环），并通过
  ``ctx.maisaka.context.append`` 把否决理由作为群友不可见的内部消息写回
  规划器聊天历史，供其下一轮思考时参考；
- 按会话维护否决计数，时间窗口内连续否决达到阈值时升级注入文案，
  要求规划器立即调用 finish 收敛，避免左右脑无限拉扯。
"""

import re
import time
from typing import Any

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

# Hook 处理器超时（毫秒）。after_response 内含一次 Runner→Host 的 RPC（context.append），
# 给出充足余量避免 Host 端默认 6s 超时截断。
HOOK_TIMEOUT_MS = 30000

# 哨兵标记名仅允许字母/数字/下划线/横线，防止用户配置破坏正则。
_SENTINEL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

DEFAULT_SENTINEL = "reject"

# 注入到 replyer prompt 的拒绝协议。占位符：{sentinel}
DEFAULT_PROTOCOL_PROMPT = (
    "你拥有一条内部否决通道：如果你判断本次回复指令（回复理由）明显有误——"
    "例如规划器要求重复回复你刚刚已经发送过的内容、把你已发送的翻译或回答误认成了别人的消息、"
    "或者指令与聊天上下文明显矛盾——请不要生成正常回复，也不要在回复里向群友解释或吐槽，"
    "只输出：<{sentinel}>简要说明否决理由</{sentinel}>，除该标记外不要输出任何其他文字。"
    "否决后这条消息不会被发送到群里，理由会通过内部通道转达给规划器。"
    "如果指令正常，请忽略本要求，正常生成回复。"
)

# 否决发生时注入规划器上下文的内部消息。占位符：{reason}、{count}
DEFAULT_INJECTION_TEMPLATE = (
    "【内部通道·仅你自己可见，群友看不到这条消息】"
    "回复器（你的另一半思维）否决了你刚才的回复指令。"
    "接下来工具结果里的\"生成可见回复失败\"并不是技术故障，而是本次否决的结果，请不要原样重试 reply。"
    "否决理由：{reason}。"
    "请结合该理由重新审视最近的聊天记录，特别注意哪些消息其实是你自己刚刚发送的；"
    "如果其实已经无需再回复，请直接调用 finish 结束本轮。"
)

# 连续否决达到阈值时的升级文案。占位符：{reason}、{count}
DEFAULT_ESCALATION_TEMPLATE = (
    "【内部通道·仅你自己可见，群友看不到这条消息】"
    "回复器已在短时间内连续 {count} 次否决你的回复指令，最新否决理由：{reason}。"
    "继续尝试回复只会再次被否决并打扰群友。"
    "请立即调用 finish 结束本轮思考，不要再调用 reply。"
)


def _render(template: str, **values: Any) -> str:
    """用占位符替换渲染模板。

    使用 ``str.replace`` 而非 ``str.format``，避免理由文本中的花括号
    导致格式化异常。
    """
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", str(value))
    return rendered


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class VetoSectionConfig(PluginConfigBase):
    """否决通道配置。"""

    __ui_label__ = "否决通道"
    __ui_icon__ = "brain"
    __ui_order__ = 1

    reject_sentinel: str = Field(
        default=DEFAULT_SENTINEL,
        description="哨兵标记名（仅字母/数字/下划线/横线）。replyer 输出 <标记>理由</标记> 即视为否决。",
    )
    max_consecutive_vetoes: int = Field(
        default=2,
        description="同一会话在时间窗口内连续否决达到该次数时，注入文案升级为\"立即 finish\"强指令。",
    )
    veto_window_seconds: int = Field(
        default=300,
        description="连续否决计数的时间窗口（秒），窗口过期后计数自动重置。",
    )
    protocol_prompt: str = Field(
        default="",
        description="注入 replyer 的拒绝协议文案，留空使用内置默认。占位符：{sentinel}。",
    )
    injection_template: str = Field(
        default="",
        description="否决时注入规划器上下文的内部消息模板，留空使用内置默认。占位符：{reason}、{count}。",
    )
    escalation_template: str = Field(
        default="",
        description="连续否决达到阈值时的升级文案模板，留空使用内置默认。占位符：{reason}、{count}。",
    )


class CorpusCallosumConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    veto: VetoSectionConfig = Field(default_factory=VetoSectionConfig)


class CorpusCallosumPlugin(MaiBotPlugin):
    """神经闭环反馈插件主体。"""

    config_model = CorpusCallosumConfig

    def __init__(self) -> None:
        super().__init__()
        # 配置派生缓存，on_load / on_config_update 时刷新
        self._enabled: bool = True
        self._sentinel: str = DEFAULT_SENTINEL
        self._sentinel_re: re.Pattern[str] = self._build_sentinel_re(DEFAULT_SENTINEL)
        self._max_consecutive_vetoes: int = 2
        self._veto_window_seconds: float = 300.0
        self._protocol_prompt: str = DEFAULT_PROTOCOL_PROMPT
        self._injection_template: str = DEFAULT_INJECTION_TEMPLATE
        self._escalation_template: str = DEFAULT_ESCALATION_TEMPLATE
        # session_id -> (连续否决次数, 最近一次否决时间戳)
        self._veto_counts: dict[str, tuple[int, float]] = {}

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def on_load(self) -> None:
        """插件加载：刷新配置缓存。"""
        self._refresh_config()
        self.ctx.logger.info(
            "神经闭环反馈插件已加载（哨兵=<%s>，升级阈值=%d 次/%.0f 秒）",
            self._sentinel,
            self._max_consecutive_vetoes,
            self._veto_window_seconds,
        )

    async def on_unload(self) -> None:
        """插件卸载：清理计数缓存。"""
        self._veto_counts.clear()
        self.ctx.logger.info("神经闭环反馈插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新：刷新派生缓存。"""
        del config_data
        del version
        if scope == "self":
            self._refresh_config()
            self.ctx.logger.info(
                "神经闭环反馈配置已更新（enabled=%s，哨兵=<%s>，升级阈值=%d 次/%.0f 秒）",
                self._enabled,
                self._sentinel,
                self._max_consecutive_vetoes,
                self._veto_window_seconds,
            )

    def _refresh_config(self) -> None:
        """从配置模型刷新派生缓存。"""
        self._enabled = bool(self.config.plugin.enabled)

        sentinel = str(self.config.veto.reject_sentinel or "").strip()
        if not _SENTINEL_NAME_RE.match(sentinel):
            if sentinel:
                self.ctx.logger.warning(
                    "哨兵标记名 %r 不合法（仅允许字母/数字/下划线/横线），回退为 %r",
                    sentinel,
                    DEFAULT_SENTINEL,
                )
            sentinel = DEFAULT_SENTINEL
        self._sentinel = sentinel
        self._sentinel_re = self._build_sentinel_re(sentinel)

        self._max_consecutive_vetoes = max(1, int(self.config.veto.max_consecutive_vetoes))
        self._veto_window_seconds = max(1.0, float(self.config.veto.veto_window_seconds))
        self._protocol_prompt = str(self.config.veto.protocol_prompt or "").strip() or DEFAULT_PROTOCOL_PROMPT
        self._injection_template = (
            str(self.config.veto.injection_template or "").strip() or DEFAULT_INJECTION_TEMPLATE
        )
        self._escalation_template = (
            str(self.config.veto.escalation_template or "").strip() or DEFAULT_ESCALATION_TEMPLATE
        )

    @staticmethod
    def _build_sentinel_re(sentinel: str) -> re.Pattern[str]:
        """构建哨兵检测正则：要求 <sentinel>...</sentinel> 锚定输出开头。"""
        escaped = re.escape(sentinel)
        return re.compile(rf"^\s*<{escaped}>(.*?)</{escaped}>", re.DOTALL)

    # ------------------------------------------------------------------ #
    # 否决计数护栏
    # ------------------------------------------------------------------ #
    def _record_veto(self, session_id: str) -> int:
        """记录一次否决并返回当前窗口内的连续否决次数。"""
        now = time.monotonic()
        count, last_ts = self._veto_counts.get(session_id, (0, 0.0))
        if now - last_ts > self._veto_window_seconds:
            count = 0
        count += 1
        self._veto_counts[session_id] = (count, now)
        return count

    def _reset_veto(self, session_id: str) -> None:
        """正常回复通过时重置该会话的连续否决计数。"""
        self._veto_counts.pop(session_id, None)

    # ------------------------------------------------------------------ #
    # Hook 1：注入拒绝协议
    # ------------------------------------------------------------------ #
    @HookHandler(
        "maisaka.replyer.before_request",
        name="inject_veto_protocol",
        description="在 replyer 请求模型前注入拒绝协议，允许其用哨兵标记否决规划器的错误回复指令。",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=HOOK_TIMEOUT_MS,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_veto_protocol(self, **kwargs: Any) -> dict[str, Any]:
        if not self._enabled:
            return {"action": "continue"}
        protocol = _render(self._protocol_prompt, sentinel=self._sentinel)
        existing = str(kwargs.get("extra_prompt") or "").strip()
        kwargs["extra_prompt"] = f"{existing}\n{protocol}" if existing else protocol
        return {"action": "continue", "modified_kwargs": kwargs}

    # ------------------------------------------------------------------ #
    # Hook 2：拦截哨兵并内部回传
    # ------------------------------------------------------------------ #
    @HookHandler(
        "maisaka.replyer.after_response",
        name="intercept_veto",
        description="检测 replyer 输出的否决哨兵：置空回复阻止发送，并将否决理由注入规划器内部上下文。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=HOOK_TIMEOUT_MS,
        error_policy=ErrorPolicy.SKIP,
    )
    async def intercept_veto(self, **kwargs: Any) -> dict[str, Any]:
        if not self._enabled:
            return {"action": "continue"}

        session_id = str(kwargs.get("session_id") or "").strip()
        response = str(kwargs.get("response") or "")
        match = self._sentinel_re.match(response)
        if match is None:
            if session_id:
                self._reset_veto(session_id)
            return {"action": "continue"}

        reason = match.group(1).strip() or "（回复器未给出具体理由）"
        count = self._record_veto(session_id) if session_id else 1
        escalated = count >= self._max_consecutive_vetoes
        template = self._escalation_template if escalated else self._injection_template
        injection_text = _render(template, reason=reason, count=count)

        self.ctx.logger.info(
            "回复器否决了规划器指令（session=%s，连续第 %d 次%s）：%s",
            session_id or "<unknown>",
            count,
            "，已升级为 finish 强指令" if escalated else "",
            reason,
        )

        if session_id:
            try:
                append_result = await self.ctx.maisaka.context.append(
                    session_id,
                    [{"type": "text", "content": injection_text}],
                    source_kind="replyer_veto",
                )
                if not (isinstance(append_result, dict) and append_result.get("success")):
                    self.ctx.logger.warning(
                        "否决理由注入规划器上下文失败（session=%s）：%s",
                        session_id,
                        append_result,
                    )
            except Exception as exc:
                self.ctx.logger.warning(
                    "否决理由注入规划器上下文异常（session=%s）：%s",
                    session_id,
                    exc,
                )
        else:
            self.ctx.logger.warning("after_response 未携带 session_id，跳过内部上下文注入")

        # 置空回复：reply 工具会静默失败，不向群里发送任何内容，
        # 思考循环继续，规划器下一轮可看到注入的否决理由。
        # 不设置 retry，避免触发 replyer 重生成循环。
        kwargs["response"] = ""
        return {"action": "continue", "modified_kwargs": kwargs}


def create_plugin() -> CorpusCallosumPlugin:
    """创建插件实例。"""
    return CorpusCallosumPlugin()
