# mcp-device-gateway 发展迭代计划

> 当前版本：v0.1.1 | 更新日期：2026-03-30

---

## 现状评估

### 已具备能力
- 5 个 MCP 工具（`device_list` / `device_ping` / `cmd_exec` / `file_upload` / `file_download`）
- 参数正则校验 + 路径白名单双重安全防护
- YAML 冻结数据类配置加载
- JSONL 格式审计日志
- PyInstaller 独立 exe 打包 + PyPI 发布流程

### 已知短板
| 领域 | 问题 |
|------|------|
| 测试 | **无任何测试**，`security.py` / `config.py` 是最高优先级缺口 |
| 安全 | 无 `known_hosts` 时静默退化为 `AutoAddPolicy`，无告警 |
| 配置 | 所有设备共享同一命令模板池，无法按设备隔离授权 |
| 可观测性 | 审计日志只写不读，无查询/汇总工具 |
| 可靠性 | 每次工具调用都建立新 SSH 连接，无连接复用 |
| 功能边界 | 无目录浏览、无文件元信息查询 |

---

## 迭代路线图

### v0.2 — 测试基础 & 配置加固

**目标**：消除"零测试"质量风险，加固配置验证逻辑。

**任务清单**

- [ ] `tests/` 目录 + pytest 环境（`pytest`, `pytest-mock`）
- [ ] `security.py` 单元测试：合法参数、注入字符、路径前缀边界、`allowed_roots=[]`
- [ ] `config.py` 单元测试：缺少字段、错误类型、空设备、模板格式
- [ ] 配置加载时预校验：模板占位符数量一致性检查（`{0}` 存在但未声明等）
- [ ] 统一测试命令写入 `pyproject.toml`（`[tool.pytest.ini_options]`）
- [ ] 补充 `CHANGELOG.md` 并建立版本条目规范

**验收标准**：`pytest` 全绿，覆盖 `security.py` + `config.py` 两个核心模块的主路径与错误路径。

---

### v0.3 — 安全增强 & 参数体系升级

**目标**：收紧两处安全弱点，支持按设备独立授权。

**任务清单**

- [ ] `AutoAddPolicy` 启用时在日志中打印 `WARNING` 并在审计记录中标注 `"host_key_policy": "auto_add"`
- [ ] 设备级命令模板覆写：`DeviceConfig` 增加可选 `command_templates` 字段，覆盖全局模板池
- [ ] 参数白名单扩展：支持在命令模板中声明 `arg_patterns`（每个占位符独立正则），比全局模式更精细
- [ ] SSH 连接参数暴露：`DeviceConfig` 支持 `connect_timeout` / `banner_timeout` / `auth_timeout` 自定义
- [ ] `sanitize_args` 测试补充 v0.3 新特性用例

**验收标准**：使用 `AutoAddPolicy` 时审计日志可见告警字段；设备 A 执行设备 B 不允许的模板时抛出 `ValueError`。

---

### v0.4 — 可观测性 & 工具扩展

**目标**：让 Agent 可以自助查询审计记录，补充文件管理工具。

**任务清单**

- [ ] 新增 MCP 工具 `audit_tail`：读取审计日志最近 N 条（默认 20），支持按 `tool` 过滤
- [ ] 新增 MCP 工具 `file_list`：列出远端目录内容（等效 `ls -la`），受 `allowed_roots` 约束
- [ ] 新增 MCP 工具 `file_stat`：查询远端文件元信息（大小、修改时间、权限），受路径白名单约束
- [ ] 审计日志字段补充：写入 `username`、`exit_code`（文件操作写 0/-1）、`bytes_transferred`
- [ ] 更新 `PLAYBOOK.md` 增加新工具使用剧本

**验收标准**：Agent 可通过 `audit_tail` 回溯近期操作；可通过 `file_list` 确认远端目录状态后再上传。

---

### v0.5 — 可靠性 & 批量操作

**目标**：提升高频操作下的性能，支持多设备批量执行。

**任务清单**

- [ ] SSH 连接池：`SshDeviceClient` 改为连接池模式（或带 TTL 的单例缓存），避免每次工具调用重建连接
- [ ] 扩展 `cmd_exec`：接受 `[(device_name, command_key, args), ...]` 列表，并发执行并汇总结果
- [ ] `file_upload` / `file_download` 支持目录递归传输（本地目录 → 远端目录，受白名单约束）
- [ ] 连接失败重试策略：可配置重试次数与退避间隔
- [ ] 新增集成测试：使用 `pytest` + Docker `linuxserver/openssh-server` 容器做真实 SSH 环境测试

**验收标准**：连接池命中时延显著降低；批量模式对 3 台设备并发执行耗时不超过单次 × 1.2。

---

### v1.0 — 生产就绪

**目标**：满足生产部署的安全、可维护、可扩展需求。

**任务清单**

- [ ] **角色权限隔离**：配置支持 `roles`，每个角色绑定允许的命令模板子集与路径白名单，设备配置引用角色
- [ ] **密钥管理集成**：`key_file` 支持从环境变量或外部密钥管理服务（如 HashiCorp Vault）读取
- [ ] **速率限制**：`cmd_exec` 支持每设备级别的调用频率上限（防止意外批量触发）
- [ ] **CI/CD 流水线**：GitHub Actions — `push` 触发 `pytest`；`tag` 触发 PyPI 发布
- [ ] **容器化部署**：提供官方 `Dockerfile` 与 `docker-compose.example.yml`
- [ ] **文档完善**：补充 MCP 注册配置示例（Cursor / Windsurf / Claude Desktop）、故障排查 FAQ

**验收标准**：CI 全绿；Docker 镜像可在 Linux 宿主机直接启动；安全审查通过 OWASP Top 10 自查表。

---

## 优先级矩阵

| 迭代 | 业务价值 | 安全影响 | 开发成本 | 推荐顺序 |
|------|---------|---------|---------|---------|
| v0.2 测试基础 | 中 | 高（发现潜在漏洞） | 低 | **立即启动** |
| v0.3 安全增强 | 中 | 高 | 中 | 第 2 优先 |
| v0.4 工具扩展 | 高 | 低 | 中 | 第 3 优先 |
| v0.5 批量操作 | 高 | 中 | 高 | 第 4 优先 |
| v1.0 生产就绪 | 高 | 高 | 高 | 最终目标 |

---

## 不在计划内的方向

以下方向**暂不纳入**本项目范围，避免功能蔓延：

- 替代完整运维平台（Ansible / Fabric）
- 图形界面或 Web 控制台
- 非 SSH 协议接入（Telnet、串口、MQTT）
- 多租户 SaaS 化
