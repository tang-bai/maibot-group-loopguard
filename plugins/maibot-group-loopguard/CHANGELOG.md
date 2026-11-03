# CHANGELOG

## 1.1.0

- 新增「频率嫌疑」识别：按「高频 + 紧跟别人发言」识别未登记的 bot，弥补昵称/名单/行为学习都抓不到的场景。判定分两步：窗口内发言次数达标 **且响应式发言占比达标**，再累计 N 次高频窗口才标记。默认关闭，建议先开演习观察。
- 修复 `check_frequency` 的变量名冲突：循环变量 `record` 覆盖了函数参数 `record: bool`，导致 `record=False` 的只读查询仍会写入嫌疑计数。
- 修复 `_last_suspect_ts` 初始值：从 `0.0` 改为 `-inf`，避免首次评估被"距离上次不足一个窗口"挡住。
- 测试 44 个：26 算法 + 15 Hook 冒烟 + 3 Schema 标签。

## 1.0.2

- 新增 `/loopguard who`：列出窗口内每个发送者被判成机器人还是人类、各自的消息条数与最后活跃时间，并给出可一键复制的 `learn` 命令。互聊拦不住时先跑这条，九成是对方账号没被识别。
- 结构性兜底：消息只 @ 已知机器人（含自身账号）时，发送者本身按机器人处理，不再依赖昵称或名单。
- 文档补充「装完还在互聊」的三步排查路径：先 `who`，再 `learn`，再看 `dry_run` / `enabled` / `platform_scope`。
- 测试 37 个：22 算法 + 12 Hook 冒烟 + 3 Schema 标签。

## 1.0.1

- 配置页汉化：39 个字段全部补上中文显示名（`json_schema_extra={"label": ...}`），WebUI 不再显示 `window_seconds`、`bigram_containment_threshold` 这类键名；悬停说明同步精简。
- 新增 `tests/test_config_labels.py`：装了真实 `maibot-plugin-sdk` 时校验「每个字段都有中文名 / 配置节标题是中文 / 字段总数与文档一致」，未装 SDK 时自动跳过。
- 打包脚本改为 `tools/build_package.py`：ZIP 条目名统一用 `/` 并内置安装器规则自检，修复 WebUI 提示「ZIP 包含非法路径、链接或加密文件」。
- 测试总数 34：21 算法 + 10 Hook 冒烟 + 3 Schema 标签。

## 1.0.0

首个版本。

- 入站拦截：`chat.receive.before_process` 上做身份识别 + 复读判定，命中即中止消息链。
- 出站兜底：`send_service.before_send` 对回复正文再查一次重，冷却期内直接取消发送。
- 身份识别：显式账号名单、行为学习（复述其它机器人内容即自动登记）、昵称正则三层。
- 复读检测：文本规范化 + SimHash（逐字重复）+ 2-gram Jaccard + 2-gram 包含度。
- 节奏检测：连续机器人消息链条、双账号交替接话、短时爆发。
- 指数退避冷却：默认 120s 起、×2 递增、1800s 封顶，人类发言立即清零。
- 可选软手段：`frequency.set_adjust` 压低回复频率、规划器摘除 `reply` 工具。
- 多实例仲裁：按账号字典序决定谁先开口，另一方提前退让。
- 状态持久化：行为学习名单与冷却状态写入 `data/plugins/<plugin_id>/loopguard_state.json`。
- 命令：`/loopguard status|reset|learn|forget|on|off`。
- 演习模式（`dry_run`）：只记日志、照常放行，用于上线前观察误杀。
