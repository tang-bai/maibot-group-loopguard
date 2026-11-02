# CHANGELOG

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
