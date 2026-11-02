# 群聊复读守卫（Group Loop Guard）

同群放了不止一个 AI 机器人时，机器人之间会互相"接话"：A 回一句、B 回一句，
几轮之后两边开始说高度相似甚至逐字重复的内容。本插件把这条链路掐掉。

## 它会做什么

| 位置 | Hook | 行为 |
| --- | --- | --- |
| 入站 | `chat.receive.before_process` | 识别机器人身份 + 复读判定，命中即 `abort`，消息不进主链、不入库、不触发推理 |
| 出站 | `send_service.before_send` | 发送前再查一次重，兜住"回复已经生成完"的那一轮 |
| 规划 | `maisaka.planner.before_request` | 可选：冷却期内摘掉 `reply` 工具，省掉一整轮推理 |
| 频率 | `frequency.set_adjust` | 可选：冷却期内把该聊天流的回复频率倍率压到 0 |

## 它凭什么判断"这是复读"

1. **身份层**：`bot_accounts` 名单 → 行为学习名单 → 昵称正则，三层依次匹配。
2. **内容层**：文本规范化（去 @ / CQ 码 / URL / 标点 / 表情）后做
   exact digest、SimHash（只认逐字重复）、2-gram Jaccard 与 2-gram 包含度。
3. **节奏层**：连续机器人消息条数、两个账号交替接话、短时间内消息爆发。
4. **退避层**：命中后按指数退避冷却（默认 120s × 2ⁿ，上限 1800s），有人类发言立即清零。

## 安装

把整个 `maibot-group-loopguard/` 目录放进 MaiBot 的 `plugins/` 下，重载插件即可。
首次加载会在插件目录生成 `config.toml`，也可以直接在 WebUI 插件配置页改。配置页字段均为中文显示名，`config.toml` 的键名保持英文。

依赖：MaiBot `1.3.x` + `maibot-plugin-sdk >= 2.0.0`（Host 自带 Runner 会拉起依赖）。

## 最小可用配置

```toml
[plugin]
enabled = true
dry_run = false

[identity]
# 同群其它机器人的 QQ 号，强烈建议显式登记
bot_accounts = ["123456789", "987654321"]
```

其余阈值保持默认即可；调参说明见仓库内 `docs/02-使用与部署.md`。

## 命令

| 命令 | 说明 |
| --- | --- |
| `/loopguard` | 查看当前聊天流的链条长度、退避档位、剩余冷却、拦截计数 |
| `/loopguard reset [stream_id]` | 重置指定流（留空重置全部） |
| `/loopguard learn <账号>` | 手动把某个账号记为机器人 |
| `/loopguard forget <账号>` | 移除行为学习记录 |
| `/loopguard on` / `off` | 本进程内临时开关 |

控制台、WebUI 始终有权限；QQ 群里需要在 `[command] command_operators` 里登记 QQ 号。

## 测试

核心算法不依赖 `maibot_sdk`，可以单独跑：

```bash
cd plugins/maibot-group-loopguard
python -m unittest discover -s tests -v
```

## 已知边界

- 语义级"换句话再说一遍"不靠指纹抓，靠连续的链条长度与交替节奏兜底；`max_chain_length` 是最终保险。
- 只约束本实例，另一台机器人需要各自装上插件（或用 `[arbiter]` 让发言权错开）。
- 拦截后的消息不会进入记忆，这是有意为之：复读内容进上下文只会让下一轮更像复读。

## 许可证

GPL-3.0-or-later，与 MaiBot 主项目一致。
