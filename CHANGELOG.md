# Changelog

本文件记录 maibot-corpus-callosum（神经闭环反馈）的版本变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [1.3.4] - 2026-08-19

### 修复

- 再审置空回复时只回写 `response`，避免 1.2.0 Item-first Host 把原样 `output_items` 当成修改而忽略正文清空

## [1.3.3] - 2026-08-01

### 变更

- 将带注释的配置模板改为 `config.default.toml`，运行期 `config.toml` 不再入库
- 在 `create_plugin` / `on_load` 中从模板补齐或恢复 Runner 生成的空壳配置

## [1.3.2] - 2026-07-11

### 修复

- 持久化配置前去除 `None`，避免 tomlkit 写入失败
- WebUI 清空可选字段时按默认值处理，不再因空字符串触发 pydantic 校验错误

## [1.3.1] - 2026-06-29

### 变更

- 默认 `protocol_prompt` 提醒 replyer：planner 可见的工具结果与插件注入思考可能不在其视图中；仅在明确矛盾时否决，勿因可被隐藏上下文解释的表面不一致而否决

## [1.3.0] - 2026-06-25

### 变更

- `<reject>` 哨兵可在 replyer 输出任意位置匹配（不再仅限开头）
- 重构内部否决反馈格式，提升可读性

### 测试

- 补充哨兵检测相关测试

## [1.2.0] - 2026-06-13

### 变更

- 空配置字段升级时跟随代码默认值；`config.toml` 增加内置 prompt 注释参考，并迁移旧版烘焙值

## [1.1.0] - 2026-06-12

### 新增

- 首次发布：通过 hooks 与 `maisaka.context.append` 为 replyer 提供对 planner 的内部否决通道，不向聊天发送消息

### 变更

- 采用「回复请求 / 聊天流」用语；拆分 escalate 与最大否决阈值；澄清注入上下文中的 planner / replyer 角色标签，避免共享历史误导 replyer
- 细化再审通道措辞、护栏与内部反馈
