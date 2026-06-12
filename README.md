# 神经闭环反馈 (maibot-corpus-callosum)

> Corpus callosum（胼胝体）——连接左右脑的神经桥。

给麦麦的 replyer（回复器）一条**内部否决通道**：当规划器（planner）指令明显有误时——例如把回复器刚翻译/发送的内容误认成别人的消息、要求重复回复——回复器可以拒绝发送，否决理由经内部上下文"闭环反馈"回规划器的思考循环，**全程群友不可见**，不再出现左右脑当众互搏、连环道歉刷屏的尴尬场面。

## 工作原理

```
规划器 reply 指令
   │
   ▼
回复器（已注入拒绝协议）
   │ 指令正常 → 正常生成回复，照常发送
   │ 指令有误 → 只输出 <reject>理由</reject>
   ▼
插件 Hook 拦截哨兵
   ├─ response 置空 → reply 工具静默失败，群里不发任何消息
   └─ 否决理由作为内部消息注入规划器聊天历史（群友不可见）
   ▼
规划器下一轮看到否决理由 → 重新审视上下文 / 调用 finish 收敛
```

具体实现：

1. **`maisaka.replyer.before_request`** Hook 通过 `extra_prompt` 注入"拒绝协议"，告知回复器可用哨兵标记否决错误指令；
2. **`maisaka.replyer.after_response`** Hook 检测输出开头的 `<reject>...</reject>`：
   - 把 `response` 改写为空串——reply 工具会返回失败结果，**不向群里发送任何内容**，且不中止思考循环；
   - 通过 `maisaka.context.append` 能力把否决理由作为内部消息追加到规划器的聊天历史；
3. **防死循环护栏**：按会话统计时间窗口内的连续否决次数，达到阈值后注入文案升级为"立即调用 finish"强指令，避免规划器和回复器无限拉扯。

插件零主程序改动，仅依赖 maibot-plugin-sdk 公开的 Hook 与能力代理。

## 安装

1. 将本插件目录复制（或软链接）到 MaiBot 的 `plugins/` 目录下：

   ```bash
   cp -r maibot-corpus-callosum /path/to/MaiBot/plugins/
   ```

2. 重启 MaiBot 或在 WebUI 插件管理中加载插件；
3. 在 WebUI 插件管理中确认"神经闭环反馈"已启用。

无额外 Python 依赖。插件声明的能力：`maisaka.context.append`。

> 提示：如果你之前在 replyer 的 prompt 里手动添加过"发现 planner 出错就公开指出"之类的临时提示词，启用本插件后建议移除，避免两套机制冲突。

## 配置（config.toml）

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `plugin.enabled` | `true` | 总开关 |
| `veto.reject_sentinel` | `"reject"` | 哨兵标记名（仅字母/数字/下划线/横线），回复器输出 `<reject>理由</reject>` 即视为否决 |
| `veto.max_consecutive_vetoes` | `2` | 同一会话窗口内连续否决达到该次数时，注入文案升级为"立即 finish"强指令 |
| `veto.veto_window_seconds` | `300` | 连续否决计数的时间窗口（秒），过期自动重置 |
| `veto.protocol_prompt` | 内置默认 | 注入回复器的拒绝协议文案，占位符 `{sentinel}` |
| `veto.injection_template` | 内置默认 | 否决时注入规划器上下文的内部消息模板，占位符 `{reason}`、`{count}` |
| `veto.escalation_template` | 内置默认 | 连续否决达阈值后的升级文案模板，占位符 `{reason}`、`{count}` |

三个模板留空时使用内置默认文案；支持配置热更新，修改后无需重启。

## 测试方式

1. **复现场景**：在测试群发一张含外文文字的图片让麦麦翻译。麦麦正常翻译后，若规划器下一轮没认出那是自己刚发的翻译、再次下达回复指令，回复器应否决该指令，群里**不再出现重复翻译或道歉刷屏**。
2. **看日志**：
   - 插件日志：`回复器否决了规划器指令（session=...，连续第 N 次）：<理由>`；
   - Host 日志：`Maisaka 回复器回复被 Hook 改写`（response 被置空）与 `回复生成器返回空文本`（reply 工具静默失败，属预期行为）。
3. **验证内部闭环**：在 WebUI 推理面板查看规划器下一轮的上下文，应包含 `source_kind=replyer_veto` 的内部消息（群友不可见）。
4. **验证护栏**：若规划器持续坚持错误指令，第 `max_consecutive_vetoes` 次否决起注入文案会升级为"立即调用 finish"，本轮思考应迅速收敛。

## 常见问题

- **群里偶尔少回了一条消息？** 查看插件日志确认是否发生了否决。若属误杀，可在 `protocol_prompt` 中收紧否决条件，或提高回复器模型质量。
- **否决后规划器还在重试 reply？** 属正常的收敛过程：第一次否决后规划器会拿到理由重新思考，若仍坚持则触发升级文案强制 finish。可调小 `max_consecutive_vetoes`（最低 1）让升级更激进。
- **修改了哨兵标记后不生效？** 标记名仅允许字母/数字/下划线/横线，不合法时回退为 `reject` 并在日志中告警。

## License

MIT
