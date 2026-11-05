# 群聊复读守卫 · Group Loop Guard for MaiBot

> 同一个 QQ 群里放了两只以上 AI 机器人时，它们会把对方当成真人来回接话，几轮之后开始复读、刷屏、白烧 token。
> 这是一个 MaiBot 插件：在消息进主链之前就把它拦住。

[![MaiBot](https://img.shields.io/badge/MaiBot-1.3.x-8fd6a0)](https://github.com/Mai-with-u/MaiBot)
[![Plugin SDK](https://img.shields.io/badge/plugin--sdk-%E2%89%A52.0-8fd6a0)](https://docs.maibot.chat/plugin/)
[![License](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](./LICENSE)
[![Tests](https://img.shields.io/badge/tests-98%20cases-brightgreen)](./plugins/maibot-group-loopguard/tests)

---

## 它解决什么

```
人类：今晚有人开黑吗
Bot-A：有的有的，我随时可以
Bot-B：那我也来，几点开始
Bot-A：那我也来，几点开始？
Bot-B：那我也来，几点开始？？
...
```

成因是三件事叠加：QQ 消息里没有可信的 `is_bot` 字段、HeartFlow / Maisaka 是持续观察式的、表达学习会让两只机器人的措辞越来越像。
于是每轮互激都是一次完整推理，复读内容还会写进聊天历史，下一轮更容易复读。

本插件把这条链路掐在三个位置：

| 位置 | Hook | 效果 |
| --- | --- | --- |
| 入站 | `chat.receive.before_process` | 判定命中即 `abort`：**不入库、不进上下文、不触发推理** |
| 出站 | `send_service.before_send` | 回复已生成也拦得住：冷却、重复或单群出站熔断时取消发送 |
| 规划 | `maisaka.planner.before_request` | 可选软静音：冷却期摘掉 `reply` 工具，省掉一整轮推理 |

---

## 判定逻辑

```
身份层   自身账号 → 真人白名单 → 机器人名单/共享名册 → 协作签名/适配器扩展 → 当前群临时学习
         多轮回声 + 不同来源内容 + bot响应锚点；昵称默认仅提示，@不定性
内容层   BLAKE2b digest（逐字）· SimHash（逐字快筛）
         2-gram Jaccard ≥ 0.72 · 2-gram 包含度 ≥ 0.85（抄一遍再补几个字）
节奏层   连续机器人消息 ≥ 4 · 双账号交替接话 · 60s 内 ≥ 6 条爆发
退避层   120s × 2ⁿ，上限 1800s；未知身份消息不清空独立出站预算
兜底层   无论发送者身份：单群60秒内最多发6条，第7条进入120秒静默；明确真人白名单可清零
```

设计原则是**第一次允许**：对面机器人说了一句正常的新内容，本实例照常回应一轮；只有进入"重复 / 交替 / 超长链条"才动手。

一段可复现的收敛过程（`python tools/loop_sim.py`，不需要 MaiBot 环境）：

```
场景 B：内容各异但节奏成环（两只机器人来回接话）
   3.0s  Bot-B 发 30002    放行            爬山听起来不错，我可能去逛展
   4.0s  Bot-A 发 30001    放行            逛展也行，最近有几个摄影展
   4.5s  Bot-A 收 30002    拦截/chain_limit 摄影展我倒是没关注过，有推荐吗
   5.0s  Bot-B 发 30002    拦截/chain_limit 摄影展我倒是没关注过，有推荐吗
   5.5s  Bot-B 收 30001    拦截/cooldown    我回头整理一份清单发群里
```

---

## 安装

```bash
# 方式一：直接放目录（推荐）
cp -r plugins/maibot-group-loopguard <MaiBot>/plugins/

# 方式二：用 Release 里的 zip
unzip maibot-group-loopguard-1.3.0.zip -d <MaiBot>/plugins/

# 方式三：自己打包（ZIP 内路径统一用 /，符合 WebUI 安装校验）
python tools/build_package.py
```

重载插件（WebUI → 插件 → 重载，或重启 `python bot.py`），Runner 日志出现下面这行即成功：

```
复读守卫已加载：enabled=True dry_run=False self=['你的Bot账号']
```

---

## 最小配置

`plugins/maibot-group-loopguard/config.toml`（也可在 WebUI 插件配置页改）：

```toml
[plugin]
enabled = true
dry_run = true          # 先演习一天，看日志里"[dry-run] 本应拦截"的文本是否都是复读

[identity]
bot_accounts = ["另一只机器人的QQ号"]   # 最可靠的识别依据
human_accounts = ["确认是真人的QQ号"]   # 真人白名单，避免误伤
trust_name_patterns = false           # 昵称只提示，不直接定性
learned_ttl_seconds = 1800            # 自动学习默认30分钟

[arbiter]
enabled = true
peers = ["另一只机器人的QQ号"]          # 两只都装时，让发言权按账号字典序错开
```

完整配置项（7 个 section、61 个字段）、场景模板与排错表见 [docs/02-使用与部署.md](./docs/02-使用与部署.md)。

> WebUI 配置页的字段名已全部汉化（Schema `label`），`config.toml` 的键名保持英文；改完重载插件并硬刷新页面即可看到。

---

## v1.3 新增：合作识别与无身份兜底

- **无须先识别bot**：默认每个聊天流60秒最多放行6条出站，第7条静默120秒；未知账号发言不能清空预算，真人白名单可清零。跨群独立、重启保留窗口计数与剩余冷却。
- **多个受控bot**：可选从宿主指定配置键同步平台:账号名册；共享密钥后还可选择给外发正文加可见的HMAC尾标，入站绑定账号/群/时间/正文验证；默认关闭外发签名。
- **适配器扩展**：`sender.bot`不是OneBot v11标准字段，默认不信任；仅确认你的适配器提供时再开启。详细配置见[部署说明](./docs/02-使用与部署.md)。

## 命令

| 命令 | 说明 |
| --- | --- |
| `/loopguard` | 查看当前流：链条长度、退避档位、剩余冷却、拦截计数 |
| `/loopguard who` | 查看当前身份依据、回声次数、已知bot响应与独立窗口 |
| `/loopguard reset [stream_id]` | 重置指定流（留空重置全部） |
| `/loopguard learn <账号>` / `forget <账号>` | 手动增删行为学习名单 |
| `/loopguard on` / `off` | 进程内临时开关 |

控制台与 WebUI 始终有权限；QQ 群内需在 `[command] command_operators` 登记。

---

## 验证

```bash
cd plugins/maibot-group-loopguard
python -m unittest discover -s tests -v     # 98 项；无真实SDK时95通过、3项Schema跳过
python ../../tools/loop_sim.py             # 双机器人收敛模拟
```

测试不依赖 MaiBot 与 `maibot-plugin-sdk`：核心算法是纯标准库，`plugin.py` 的 Hook 用 `tests/stubs/` 顶替 SDK 跑通。

---

## 目录结构

```
.
├── docs/
│   ├── 01-方案设计.md          # 问题建模、源码级可行性、算法实测、状态机、灰度与验收
│   └── 02-使用与部署.md        # 安装、配置全表、场景模板、命令、排错、验收步骤
├── plugins/maibot-group-loopguard/
│   ├── _manifest.json          # Manifest v2（id: maibot-community.group-loop-guard）
│   ├── plugin.py               # Hook 适配 / 身份识别 / 持久化 / 命令
│   ├── loopguard_core.py       # 纯标准库算法：规范化、指纹、状态机、仲裁
│   ├── tests/                  # 98 项用例 + SDK 替身
│   ├── README.md
│   └── CHANGELOG.md
└── tools/
    ├── loop_sim.py             # 双机器人收敛模拟
    └── build_package.py        # 打包成可安装 zip（自检路径规则）
```

---

## 兼容性

| 项 | 要求 |
| --- | --- |
| MaiBot | 1.3.0 ~ 1.3.99（源码核对版本 v1.3.19，commit `c642954`） |
| 插件 SDK | ≥ 2.0.0（Host 的 Runner 自带，插件侧无需 pip 安装） |
| 平台 | 默认 `qq`，可通过 `identity.platform_scope` 扩展 |

---

## 已知边界

- **语义级改写**不靠指纹抓，靠链条长度与交替节奏兜底；`max_chain_length` 是最终保险。
- **单边生效**：只装一边也能显著降频（对方连发会持续撞冷却），但两边都装才对称。
- **身份是启发式判断**：聊天文本本身不能证明真实身份。默认先观察未知账号；单次复制、@、昵称都不直接判bot。
- **不复读的未知bot**：已知bot显式填 bot_accounts 最可靠；未知身份仍受独立出站熔断约束，避免无限自动回复。可选频率嫌疑须多窗口响应已知bot，链条上限只对已识别bot生效。
- **升级**：v1旧学习名单和冷却自动清理，手工临时learn也需重新登记；配置中的bot_accounts保留。
- **拦截即不入记忆**：这是有意为之，复读内容进上下文只会让下一轮更像复读。

## 常见问题

**WebUI 装 ZIP 报「ZIP 包含非法路径、链接或加密文件：maibot-group-loopguard\CHANGELOG.md」**
Windows 的 `Compress-Archive` 会把条目名写成反斜杠（`maibot-group-loopguard\CHANGELOG.md`），而
MaiBot 的安装器（`src/webui/routers/plugin/zip_install.py:45`）检查 `"\\" in name` 就直接拒绝。
用 `python tools/build_package.py` 重新打包即可 —— 它用 `zipfile` 写入 `/` 分隔的条目，并在打包后按同一套规则自检。
已经打好的包也可以先解压、再把目录直接拷进 `<MaiBot>/plugins/`。

---

## 文档

- [方案设计：为什么这样做、可行性如何](./docs/01-方案设计.md)
- [使用与部署：怎么装、怎么调、出错怎么办](./docs/02-使用与部署.md)
- [插件说明](./plugins/maibot-group-loopguard/README.md) · [更新日志](./plugins/maibot-group-loopguard/CHANGELOG.md)

## 许可

GPL-3.0-or-later，与 [MaiBot](https://github.com/Mai-with-u/MaiBot) 主项目一致。
