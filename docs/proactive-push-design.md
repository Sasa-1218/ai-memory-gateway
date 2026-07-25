# Proactive Push Design

本文记录当前主动推送系统的设计边界和后续演进方向。它是设计文档，不是开发日志。

## 设计目标

主动推送不是固定频率发送，也不是定时提醒系统。它的目标是在合适的时候主动靠近用户，让 AI 像自己想开口一样自然出现。

成本优化的目标是减少无意义模型调用，尤其是重复判断和明显不合适时机的判断；它不是为了降低主动性，也不是把主动联系改成更少出现。

## 当前主动推送流程

当前流程按顺序执行：

1. 外部 cron 定期调用主动推送 trigger endpoint。
2. 网关代码先执行硬规则检查，包括静默时间、最近生成保护、最短间隔和每日上限等。
3. 硬规则通过后，执行 decision cooldown 检查，避免在短时间内反复调用模型做同类判断。
4. cooldown 允许后，调用当前聊天主模型进行结构化 `send` / `skip` 判断。
5. 当模型返回 `skip` 时，只记录决策，不保存主动消息，不进入 Bark 推送流程。
6. 当模型返回 `send` 且消息有效时，将最终主动消息保存到主 session，然后进入现有推送发送流程。

模型无权绕过代码硬规则。代码硬规则通过也不代表必须发送，最终仍由模型判断此刻是否自然。

## 当前成本优化机制

### blocked 与 skip

`blocked` 表示模型调用前已经被代码规则拦截，例如静默时间、最近生成保护、每日上限或 decision cooldown。它通常不产生模型 token 消耗。

`skip` 表示代码规则允许进入模型判断，但模型认为此刻不适合主动开口。它会产生一次模型判断成本，并记录模型返回的简短 reason。

### decision cooldown

decision cooldown 用于减少重复模型判断：

- 普通判断后：30 分钟
- 连续 `skip` 后：
  - 1 次：60 分钟
  - 2 次：90 分钟
  - 3 次及以上：120 分钟封顶

cooldown 只减少重复模型判断，不永久禁止主动推送。用户发来新消息、明确 hard event 或明确的重要事件可以解除 decision cooldown。

连续 `skip` 只影响下一次 decision cooldown 长度，不等于用户未回复就禁止主动联系。

## important_dates 设计

`important_dates` 用于记录关系中的重要日期事件，并在必要时避免 decision cooldown 错过重要主动联系机会。

核心字段：

- `event_type`：事件类别。
- `importance`：事件重要等级。
- `cooldown_bypass`：是否允许解除 decision cooldown。

设计边界：

- `cooldown_bypass` 只解除 decision cooldown。
- 它不会自动生成消息。
- 它不会直接触发 Bark。
- 它不会绕过静默时间、每日上限或其他硬规则。
- cooldown 解除后，仍由聊天主模型判断 `send` 或 `skip`。

`important_dates` 不是完整日历系统，也不是提醒系统。当前只作为主动推送判断前的状态辅助。

## Shadow Mind Phase A

Shadow Mind Phase A 是可观察的内在状态层。当前只计算和记录状态，不影响主动推送。

当前保证：

- 不调用模型。
- 不产生 token 消耗。
- 不修改 `send` / `skip` 判断。
- 不修改 GPT prompt。
- 不触发主动推送。
- 不触发 Bark。

新增状态存储：

- `shadow_mind_state`：当前 drive 状态。
- `thought_pool`：脱敏的候选 thought code，不保存正文。
- `drive_event_log`：drive 数值变化日志。

当前 drive：

- `longing`
- `curiosity`
- `share`
- `warmth`
- `concern`

`concern` 只基于已知交互事实计算，例如沉默时长和连续未回应推送数量。它不推断健康、危险、位置或现实状态。

## 外部定时任务

主动推送由外部 cron 服务触发，频率不受仓库代码或 Coolify 部署自动管理。

当前要求：

- 外部触发频率：每30分钟
- 修改网关代码不会自动修改外部 cron 配置
- 部署主动推送相关改动后，必须单独检查外部 cron 是否：
  - 已启用或关闭
  - 频率正确
  - 请求地址正确
  - 没有重复任务

在调试或修改主动推送成本逻辑期间，应先暂停外部 cron，完成部署和验证后再恢复。

## 后续规划（未实现）

### Shadow Mind Phase B

未来可以让 Shadow Mind 参与候选判断，例如提供候选意图、关系修复状态或近期主题倾向。但它不应直接绕过代码硬规则，也不应直接触发推送。

### decision 模型优化

未来可以评估低成本模型承担 `send` / `skip` 判断。但需要先做 shadow 对比验证，确认低成本模型不会显著降低关系感、自然度或安全性。

### IO 接入

IO 感知数据未来可以产生 candidate，例如设备状态、环境状态或用户授权后的上下文变化。但 IO 不应直接触发主动推送，仍需要经过网关的规则层、状态层和模型判断。

## 当前明确未启用

- Shadow Mind 控制发送。
- IO 主动触发。
- 主动推送 decision 模型替换。
- 自动提醒系统。
- 根据健康、位置或设备状态自动判断现实危险。
