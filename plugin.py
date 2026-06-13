"""神经闭环反馈 (maibot-corpus-callosum) 插件。

给 replyer 一条内部再审通道（"左右脑内部链接"）：

- 在 ``maisaka.replyer.before_request`` 注入"再审协议"提示词，告知 replyer
  当规划器提供的回复请求与回复器掌握的聊天流严重不符时，可只输出
  ``<reject>理由</reject>`` 哨兵标记，驳回发送并要求规划器重新思考；
- 在 ``maisaka.replyer.after_response`` 拦截哨兵：把 ``response`` 置空使
  reply 工具静默失败（不向聊天流发送任何内容、不中止思考循环），并通过
  ``ctx.maisaka.context.append`` 把再审理由作为聊天对象不可见的内部消息写回
  规划器聊天历史，供其下一轮重新思考时参考；
- 按会话维护再审计数：达到 ``escalate_consecutive_vetoes`` 时向规划器注入升级警示文案；
  达到 ``max_consecutive_vetoes`` 后不再向 replyer 注入再审协议、也不再拦截哨兵，
  强制其按正常流程生成回复，避免内部空转。
"""

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

# Hook 处理器超时（毫秒）。after_response 内含一次 Runner→Host 的 RPC（context.append），
# 给出充足余量避免 Host 端默认 6s 超时截断。
HOOK_TIMEOUT_MS = 30000

# 哨兵标记名仅允许字母/数字/下划线/横线，防止用户配置破坏正则。
_SENTINEL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

DEFAULT_SENTINEL = "reject"

CURRENT_CONFIG_VERSION = "1.2.0"

DEFAULT_ESCALATE_CONSECUTIVE_VETOES = 2
DEFAULT_MAX_CONSECUTIVE_VETOES = 5
DEFAULT_VETO_WINDOW_SECONDS = 300

# 注入到 replyer prompt 的再审协议。占位符：{sentinel}
DEFAULT_PROTOCOL_PROMPT = (
    "你拥有一条内部再审通道。你收到的回复理由与回复参考信息来自\"规划器\"——"
    "内部负责分析聊天并决定何时回复的决策过程（它不是你，也看不到这条说明）。"
    "当你认为规划器提供的回复请求与你掌握的聊天流严重不符时，"
    "可以驳回本次回复并要求规划器重新思考，附上再审理由。"
    "典型情形包括：规划器把你自己发送的消息错认成了别人的消息；"
    "规划器没有理解你之前发送某条消息的理由或意义；"
    "规划器未见你发送的全部消息（如开启了智能分段），误以为表达有所缺失而再度调用回复试图补全；"
    "规划器思考期间聊天流已有变化，或聊天对象输入有误，致使回复请求失准；"
    "由于模型能力差别，规划器对聊天流的理解与你有明显差别，致使回复请求失准；等等。"
    "此时请不要生成正常回复，也不要在回复里向聊天对象解释或吐槽，"
    "只输出：<{sentinel}>简要说明再审理由</{sentinel}>，除该标记外不要输出任何其他文字。"
    "驳回后这条消息不会被发送到聊天流，再审理由会通过内部通道转达给规划器，要求其重新思考。"
    "如果回复请求与聊天流相符，请忽略本要求，正常生成回复。"
)

# 触发再审时注入规划器上下文的内部消息。占位符：{reason}、{count}
DEFAULT_INJECTION_TEMPLATE = (
    "【内部再审反馈·不会发送到聊天流】"
    "（角色说明：\"规划器\"指负责分析聊天并调用 reply、finish 等工具做决策的内部过程，"
    "即本条消息的接收方；\"回复器\"指依据回复请求实际撰写回复文本的内部过程。"
    "若正在阅读的你只负责撰写回复而不能调用工具，本条消息无需你处理。）"
    "回复器认为规划器刚才的回复请求与其掌握的聊天流严重不符，"
    "已驳回本次回复并触发再审，要求规划器重新思考。"
    "随后工具结果中的\"生成可见回复失败\"并非技术故障，而是本次再审驳回的结果；"
    "规划器请勿原样重试 reply。"
    "再审理由：{reason}。"
    "请规划器结合该理由重新审视聊天流：特别注意哪些消息其实是规划器自己刚刚发送的、"
    "此前发送某条消息的理由或意义、以及是否出现了尚未注意到的新消息。"
    "请规划器结合上述再审理由和当前聊天流重新判断，再决定下一步采取何种工具或行动。"
    "若规划器选择再次调用 reply，须修正回复请求、更新回复参考信息，"
    "并确保已回应驳回理由，不要原样重试。"
)

# 连续触发再审达到阈值时的升级文案。占位符：{reason}、{count}
DEFAULT_ESCALATION_TEMPLATE = (
    "【内部再审反馈·不会发送到聊天流】"
    "（角色说明：\"规划器\"指负责分析聊天并调用 reply、finish 等工具做决策的内部过程，"
    "即本条消息的接收方；\"回复器\"指依据回复请求实际撰写回复文本的内部过程。"
    "若正在阅读的你只负责撰写回复而不能调用工具，本条消息无需你处理。）"
    "回复器已在短时间内连续 {count} 次触发再审，最新再审理由：{reason}。"
    "这表明规划器的回复请求与聊天流持续不符；在未厘清驳回理由前，规划器若反复调用 reply，"
    "虽不会向聊天流发送消息，却会继续在内部被驳回，浪费思考轮次与算力。"
    "请规划器结合历次驳回理由和当前聊天流重新审视局势，审慎选择下一步的工具或行动，"
    "不要原样重试。"
    "若规划器仍决定调用 reply，须修正回复请求、更新回复参考信息，并充分回应历次驳回理由。"
    "若规划器审视后确认当前不宜再回复，可调用 finish 结束本轮。"
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
    config_version: str = Field(default=CURRENT_CONFIG_VERSION, description="配置版本")


class VetoSectionConfig(PluginConfigBase):
    """再审通道配置。"""

    __ui_label__ = "再审通道"
    __ui_icon__ = "brain"
    __ui_order__ = 1

    reject_sentinel: str = Field(
        default="",
        description="哨兵标记名（仅字母/数字/下划线/横线）。replyer 输出 <标记>理由</标记> 即视为触发再审。留空使用插件内置默认。",
        json_schema_extra={"placeholder": DEFAULT_SENTINEL},
    )
    escalate_consecutive_vetoes: int | None = Field(
        default=None,
        ge=1,
        description=(
            "同一会话在时间窗口内连续触发再审达到该次数时，向规划器注入升级警示文案"
            "（替代普通再审反馈）。留空使用插件内置默认。"
        ),
        json_schema_extra={"placeholder": str(DEFAULT_ESCALATE_CONSECUTIVE_VETOES)},
    )
    max_consecutive_vetoes: int | None = Field(
        default=None,
        ge=1,
        description=(
            "同一会话在时间窗口内连续触发再审达到该次数后，不再向 replyer 注入再审协议，"
            "也不再拦截哨兵，强制其按正常流程生成回复。留空使用插件内置默认。"
        ),
        json_schema_extra={"placeholder": str(DEFAULT_MAX_CONSECUTIVE_VETOES)},
    )
    veto_window_seconds: int | None = Field(
        default=None,
        ge=1,
        description="连续触发再审计数的时间窗口（秒），窗口过期后计数自动重置。留空使用插件内置默认。",
        json_schema_extra={"placeholder": str(DEFAULT_VETO_WINDOW_SECONDS)},
    )
    protocol_prompt: str = Field(
        default="",
        description="注入 replyer 的再审协议文案，留空使用内置默认。占位符：{sentinel}。",
        json_schema_extra={"placeholder": DEFAULT_PROTOCOL_PROMPT},
    )
    injection_template: str = Field(
        default="",
        description="触发再审时注入规划器上下文的内部消息模板，留空使用内置默认。占位符：{reason}、{count}。",
        json_schema_extra={"placeholder": DEFAULT_INJECTION_TEMPLATE},
    )
    escalation_template: str = Field(
        default="",
        description="达到 escalate_consecutive_vetoes 时注入规划器的升级文案模板，留空使用内置默认。占位符：{reason}、{count}。",
        json_schema_extra={"placeholder": DEFAULT_ESCALATION_TEMPLATE},
    )


class CorpusCallosumConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    veto: VetoSectionConfig = Field(default_factory=VetoSectionConfig)


# --------------------------------------------------------------------------- #
# 配置解析（空值 = 使用代码内置默认，便于版本升级后自动跟随新默认）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EffectiveVetoConfig:
    """运行时生效的再审配置（已解析占位空值）。"""

    reject_sentinel: str
    escalate_consecutive_vetoes: int
    max_consecutive_vetoes: int
    veto_window_seconds: int
    protocol_prompt: str
    injection_template: str
    escalation_template: str


def _effective_int(value: int | None, default: int, *, minimum: int = 1) -> int:
    if value is None:
        return default
    return max(minimum, int(value))


def _effective_template(value: str | None, default: str) -> str:
    if value is None or not str(value).strip():
        return default
    return str(value)


def _normalize_sentinel(value: str | None) -> str:
    sentinel = str(value or "").strip()
    if not sentinel or not _SENTINEL_NAME_RE.match(sentinel):
        return DEFAULT_SENTINEL
    return sentinel


def resolve_effective_veto_config(veto: VetoSectionConfig) -> EffectiveVetoConfig:
    max_vetoes = _effective_int(veto.max_consecutive_vetoes, DEFAULT_MAX_CONSECUTIVE_VETOES)
    escalate = _effective_int(veto.escalate_consecutive_vetoes, DEFAULT_ESCALATE_CONSECUTIVE_VETOES)
    if escalate > max_vetoes:
        escalate = max_vetoes
    return EffectiveVetoConfig(
        reject_sentinel=_normalize_sentinel(veto.reject_sentinel),
        escalate_consecutive_vetoes=escalate,
        max_consecutive_vetoes=max_vetoes,
        veto_window_seconds=_effective_int(veto.veto_window_seconds, DEFAULT_VETO_WINDOW_SECONDS),
        protocol_prompt=_effective_template(veto.protocol_prompt, DEFAULT_PROTOCOL_PROMPT),
        injection_template=_effective_template(veto.injection_template, DEFAULT_INJECTION_TEMPLATE),
        escalation_template=_effective_template(veto.escalation_template, DEFAULT_ESCALATION_TEMPLATE),
    )


_LEGACY_BAKED_VETO_DEFAULTS: dict[str, int | str] = {
    "reject_sentinel": DEFAULT_SENTINEL,
    "escalate_consecutive_vetoes": DEFAULT_ESCALATE_CONSECUTIVE_VETOES,
    "max_consecutive_vetoes": DEFAULT_MAX_CONSECUTIVE_VETOES,
    "veto_window_seconds": DEFAULT_VETO_WINDOW_SECONDS,
    "protocol_prompt": DEFAULT_PROTOCOL_PROMPT,
    "injection_template": DEFAULT_INJECTION_TEMPLATE,
    "escalation_template": DEFAULT_ESCALATION_TEMPLATE,
}


def _migrate_legacy_baked_defaults(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """将旧版 config.toml 中写死的默认值还原为占位空值，以便跟随代码内置默认。"""
    veto = config.get("veto")
    if not isinstance(veto, dict):
        return config, False

    changed = False
    for key, legacy_value in _LEGACY_BAKED_VETO_DEFAULTS.items():
        if key not in veto:
            continue
        current = veto[key]
        if isinstance(legacy_value, str):
            if str(current) != legacy_value:
                continue
            veto[key] = ""
        elif current == legacy_value:
            veto[key] = None
        else:
            continue
        changed = True

    plugin_section = config.get("plugin")
    if isinstance(plugin_section, dict):
        plugin_section["config_version"] = CURRENT_CONFIG_VERSION

    return config, changed


class CorpusCallosumPlugin(MaiBotPlugin):
    """神经闭环反馈插件主体。"""

    config_model = CorpusCallosumConfig

    def __init__(self) -> None:
        super().__init__()
        # 配置派生缓存，on_load / on_config_update 时刷新
        self._enabled: bool = True
        self._sentinel: str = DEFAULT_SENTINEL
        self._sentinel_re: re.Pattern[str] = self._build_sentinel_re(DEFAULT_SENTINEL)
        self._escalate_consecutive_vetoes: int = DEFAULT_ESCALATE_CONSECUTIVE_VETOES
        self._max_consecutive_vetoes: int = DEFAULT_MAX_CONSECUTIVE_VETOES
        self._veto_window_seconds: float = float(DEFAULT_VETO_WINDOW_SECONDS)
        self._protocol_prompt: str = DEFAULT_PROTOCOL_PROMPT
        self._injection_template: str = DEFAULT_INJECTION_TEMPLATE
        self._escalation_template: str = DEFAULT_ESCALATION_TEMPLATE
        # session_id -> (连续触发再审次数, 最近一次触发时间戳)
        self._veto_counts: dict[str, tuple[int, float]] = {}

    def normalize_plugin_config(
        self, config_data: Mapping[str, Any] | None
    ) -> tuple[dict[str, Any], bool]:
        normalized, changed = super().normalize_plugin_config(config_data)
        migrated, migrated_changed = _migrate_legacy_baked_defaults(normalized)
        return migrated, changed or migrated_changed

    def _effective_veto(self) -> EffectiveVetoConfig:
        return resolve_effective_veto_config(self.config.veto)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def on_load(self) -> None:
        """插件加载：刷新配置缓存。"""
        self._refresh_config()
        self.ctx.logger.info(
            "神经闭环反馈插件已加载（哨兵=<%s>，升级=%d 次，停用再审=%d 次/%.0f 秒）",
            self._sentinel,
            self._escalate_consecutive_vetoes,
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
                "神经闭环反馈配置已更新（enabled=%s，哨兵=<%s>，升级=%d 次，停用再审=%d 次/%.0f 秒）",
                self._enabled,
                self._sentinel,
                self._escalate_consecutive_vetoes,
                self._max_consecutive_vetoes,
                self._veto_window_seconds,
            )

    def _refresh_config(self) -> None:
        """从配置模型刷新派生缓存。"""
        self._enabled = bool(self.config.plugin.enabled)
        effective = self._effective_veto()

        configured_sentinel = str(self.config.veto.reject_sentinel or "").strip()
        if configured_sentinel and not _SENTINEL_NAME_RE.match(configured_sentinel):
            self.ctx.logger.warning(
                "哨兵标记名 %r 不合法（仅允许字母/数字/下划线/横线），回退为 %r",
                configured_sentinel,
                DEFAULT_SENTINEL,
            )

        self._sentinel = effective.reject_sentinel
        self._sentinel_re = self._build_sentinel_re(effective.reject_sentinel)
        self._max_consecutive_vetoes = effective.max_consecutive_vetoes
        self._escalate_consecutive_vetoes = effective.escalate_consecutive_vetoes
        if self._escalate_consecutive_vetoes > self._max_consecutive_vetoes:
            self.ctx.logger.warning(
                "escalate_consecutive_vetoes（%d）大于 max_consecutive_vetoes（%d），"
                "已按 max 对齐",
                self._escalate_consecutive_vetoes,
                self._max_consecutive_vetoes,
            )
            self._escalate_consecutive_vetoes = self._max_consecutive_vetoes
        self._veto_window_seconds = float(effective.veto_window_seconds)
        self._protocol_prompt = effective.protocol_prompt
        self._injection_template = effective.injection_template
        self._escalation_template = effective.escalation_template

    @staticmethod
    def _build_sentinel_re(sentinel: str) -> re.Pattern[str]:
        """构建哨兵检测正则：要求 <sentinel>...</sentinel> 锚定输出开头。"""
        escaped = re.escape(sentinel)
        return re.compile(rf"^\s*<{escaped}>(.*?)</{escaped}>", re.DOTALL)

    # ------------------------------------------------------------------ #
    # 再审计数护栏
    # ------------------------------------------------------------------ #
    def _get_veto_count(self, session_id: str) -> int:
        """返回当前窗口内的连续触发再审次数（不递增）。"""
        if not session_id:
            return 0
        now = time.monotonic()
        count, last_ts = self._veto_counts.get(session_id, (0, 0.0))
        if now - last_ts > self._veto_window_seconds:
            return 0
        return count

    def _record_veto(self, session_id: str) -> int:
        """记录一次触发再审并返回当前窗口内的连续次数。"""
        now = time.monotonic()
        count = self._get_veto_count(session_id)
        count += 1
        self._veto_counts[session_id] = (count, now)
        return count

    def _reset_veto(self, session_id: str) -> None:
        """正常回复通过时重置该会话的连续再审计数。"""
        self._veto_counts.pop(session_id, None)

    # ------------------------------------------------------------------ #
    # Hook 1：注入再审协议
    # ------------------------------------------------------------------ #
    @HookHandler(
        "maisaka.replyer.before_request",
        name="inject_veto_protocol",
        description="在 replyer 请求模型前注入再审协议，允许其在回复请求与聊天流严重不符时用哨兵标记驳回并要求重新思考。",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=HOOK_TIMEOUT_MS,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_veto_protocol(self, **kwargs: Any) -> dict[str, Any]:
        if not self._enabled:
            return {"action": "continue"}
        session_id = str(kwargs.get("session_id") or "").strip()
        if session_id and self._get_veto_count(session_id) >= self._max_consecutive_vetoes:
            self.ctx.logger.info(
                "已连续触发再审 %d 次，本次不再向 replyer 注入再审协议（session=%s）",
                self._max_consecutive_vetoes,
                session_id,
            )
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
        description="检测 replyer 输出的再审哨兵：置空回复阻止发送，并将再审理由注入规划器内部上下文。",
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

        if session_id and self._get_veto_count(session_id) >= self._max_consecutive_vetoes:
            self.ctx.logger.info(
                "已连续触发再审 %d 次，本次不再拦截哨兵，按 replyer 原样输出（session=%s）",
                self._max_consecutive_vetoes,
                session_id,
            )
            return {"action": "continue"}

        reason = match.group(1).strip() or "（回复器未给出具体理由）"
        count = self._record_veto(session_id) if session_id else 1
        escalated = count >= self._escalate_consecutive_vetoes
        template = self._escalation_template if escalated else self._injection_template
        injection_text = _render(template, reason=reason, count=count)

        self.ctx.logger.info(
            "回复器触发了再审（session=%s，连续第 %d 次%s）：%s",
            session_id or "<unknown>",
            count,
            "，已注入升级警示文案" if escalated else "",
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
                        "再审理由注入规划器上下文失败（session=%s）：%s",
                        session_id,
                        append_result,
                    )
            except Exception as exc:
                self.ctx.logger.warning(
                    "再审理由注入规划器上下文异常（session=%s）：%s",
                    session_id,
                    exc,
                )
        else:
            self.ctx.logger.warning("after_response 未携带 session_id，跳过内部上下文注入")

        # 置空回复：reply 工具会静默失败，不向聊天流发送任何内容，
        # 思考循环继续，规划器下一轮可看到注入的再审理由。
        # 不设置 retry，避免触发 replyer 重生成循环。
        kwargs["response"] = ""
        return {"action": "continue", "modified_kwargs": kwargs}


def create_plugin() -> CorpusCallosumPlugin:
    """创建插件实例。"""
    return CorpusCallosumPlugin()
