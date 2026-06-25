# 神经闭环反馈 (maibot-corpus-callosum)

> Corpus callosum（胼胝体）——连接左右脑的神经桥。

给麦麦的 replyer（回复器）一条**内部再审通道**：当回复器（replyer）认为规划器（planner）提供的**回复请求**与回复器掌握的**聊天流**严重不符时，可以驳回发送，要求规划器重新思考并附上再审理由。

> **回复请求**指规划器通过 reply 工具下发的回复决策与回复参考信息；**回复参考信息**特指其中的 `reference_info`；**聊天流**是回复器掌握的上下文（含聊天历史等），用于与回复请求对照。

> 作为麦麦本体的回复器：***神经***！进行一个闭环反馈！

可能用到驳回再审的典型情形例如：

- 规划器把回复器自己发送的消息错认为别人的消息；
- 规划器没有理解回复器之前发送某条消息的理由/意义；
- 规划器未见回复器发送的全部消息（如开启了智能分段），误以为表达有所缺失而再度调用 reply 试图补全；
- 规划器思考期间聊天流已有变化（如聊天对象输入有误）致使回复请求失准；
- 由于模型多模能力差别，规划器对聊天流的理解与回复器有明显差别，致使回复请求失准；等等。

再审理由经内部上下文"闭环反馈"回规划器的思考循环，**全程聊天对象不可见**，不再出现左右脑当众互搏、连环道歉刷屏的尴尬场面。

> 悄悄地进行左右脑互搏，刷屏地不要

## 工作原理

```
规划器 reply 回复请求
   │
   ▼
回复器（已注入再审协议）
   │ 回复请求与聊天流相符 → 正常生成回复，照常发送
   │ 回复请求与聊天流严重不符 → 只输出 <reject>再审理由</reject>
   ▼
插件 Hook 拦截哨兵
   ├─ response 置空 → reply 工具静默失败，不向聊天流发送任何消息
   └─ 再审理由作为内部消息注入规划器聊天历史（聊天对象不可见）
   ▼
规划器下一轮看到再审理由 → 重新思考回复内容 / 调用 finish 收敛
```

具体实现：

1. `**maisaka.replyer.before_request**` Hook 通过 `extra_prompt` 注入"再审协议"，告知回复器在回复请求与聊天流严重不符时可用哨兵标记驳回并要求重新思考；
2. `**maisaka.replyer.after_response**` Hook 检测回复中的 `<reject>...</reject>`：
  - 把 `response` 改写为空串——reply 工具会返回失败结果，**不向聊天流发送任何内容**，且不中止思考循环；
  - 通过 `maisaka.context.append` 能力把再审理由作为内部消息追加到规划器的聊天历史；
3. **防死循环护栏**（`veto_window_seconds` 窗口内按会话计数）：
   - 第 1 次触发再审：向规划器注入普通再审反馈；
   - 达到 `escalate_consecutive_vetoes`（默认 2 次）：注入升级警示文案；
   - 达到 `max_consecutive_vetoes`（默认 5 次）后：不再向 replyer 注入再审协议、不再拦截哨兵，强制按正常流程生成回复，避免内部空转浪费算力。

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

`[veto]` 下的数值与模板字段**留空或不写**即使用插件内置默认；仅在你需要覆盖时再填写。插件升级后若内置默认变更，留空字段会自动跟随，无需手动改配置。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `plugin.enabled` | `true` | 总开关 |
| `veto.reject_sentinel` | `"reject"` | 哨兵标记名，回复器输出 `<reject>理由</reject>` 即视为触发再审 |
| `veto.escalate_consecutive_vetoes` | `2` | 窗口内连续触发再审达到该次数时，向规划器注入升级警示文案 |
| `veto.max_consecutive_vetoes` | `5` | 窗口内连续触发再审达到该次数后，停用 replyer 再审能力，强制正常生成回复 |
| `veto.veto_window_seconds` | `300` | 连续再审计数的时间窗口（秒），过期自动重置 |
| `veto.protocol_prompt` | 内置默认 | 注入 replyer 的再审协议文案，占位符 `{sentinel}` |
| `veto.injection_template` | 内置默认 | 触发再审时注入规划器的内部消息模板，占位符 `{reason}`、`{count}` |
| `veto.escalation_template` | 内置默认 | 达到 `escalate_consecutive_vetoes` 时的升级文案模板，占位符 `{reason}`、`{count}` |


三个模板留空时使用内置默认文案；支持配置热更新，修改后无需重启。

## 测试方式

1. **复现场景**：
  - 配置了多模态planner和replyer并开启多模态消息的情况下，在测试聊天流发一张含外文文字的图片让麦麦翻译。麦麦正常翻译后，若规划器下一轮没认出那是自己刚发的翻译、再次下达回复请求，回复器应触发再审并要求规划器重新思考，聊天流中**不再出现重复翻译或道歉刷屏**。
  - 开启智能分段插件，调大参数，让麦麦写一则几百字的故事，如果planner输入了不完整的故事可能会让replyer继续输出剩下的（其实只是智能分段没发完呢）。此时如果replyer接到planner消息时智能分段已经发完，replyer可能会驳回续写请求。
2. **看日志**：
  - 插件日志：`回复器触发了再审（session=...，连续第 N 次）：<理由>`；
  - Host 日志：`Maisaka 回复器回复被 Hook 改写`（response 被置空）与 `回复生成器返回空文本`（reply 工具静默失败，属预期行为）。
3. **验证内部闭环**：在 WebUI 推理面板查看规划器下一轮的上下文，应包含 `source_kind=replyer_veto` 的内部消息（聊天对象不可见）。
4. **验证护栏**：连续触发再审时，第 `escalate_consecutive_vetoes` 次起应看到升级警示文案；达到 `max_consecutive_vetoes` 后日志应出现「不再向 replyer 注入再审协议」。

## 常见问题

- **聊天流中偶尔少回了一条消息？** 查看插件日志确认是否触发了再审。若属误杀，可在 `protocol_prompt` 中收紧触发条件，或提高回复器模型质量。
- **触发再审后规划器还在重试 reply？** 属正常的收敛过程：达到 `escalate_consecutive_vetoes` 后会注入升级警示文案。若内部空转过多，可调小 `max_consecutive_vetoes` 让 replyer 更早停用再审、强制正常输出。
- **修改了哨兵标记后不生效？** 标记名仅允许字母/数字/下划线/横线，不合法时回退为 `reject` 并在日志中告警。

## License

MIT
