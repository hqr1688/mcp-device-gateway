# mcp-device-gateway 长期产品愿景

> 更新日期：2026-03-30

---

## 核心定位

**让 AI Agent 成为嵌入式/工控开发者的"安全副驾"。**

不是替代工程师，而是在工程师划定的安全边界内，让 AI 帮助完成重复性、跨设备、高频率的操作——固件下发、日志拉取、状态检查、测试验证——同时保留完整的审计链路和操作可追溯性。

### 目标用户
- 嵌入式固件/BSP 开发工程师（日常编译→烧录→验证循环）
- 工控系统集成商（多台 PLC/ARM 工控机统一管理）
- 测试自动化工程师（多设备并发测试、结果收集）
- 系统可靠性工程师（SRE）负责维护工业 Linux 网关

### 与通用 SSH MCP 的本质区别
| 维度 | 通用 SSH MCP | 本项目 |
|------|-------------|--------|
| 命令执行 | Agent 拼接任意 shell | 只能调用预定义命令模板 |
| 文件访问 | 无限制 | `allowed_roots` 白名单 |
| 多设备 | 单连接 | 设备池抽象，命名管理 |
| 场景语义 | 通用运维 | 嵌入式开发/工控维护 |
| 审计 | 无或简单日志 | JSONL 结构化审计 |

---

## 战略阶段划分

```
2026 Q1-Q2  →  阶段一：地基整固
2026 Q3-Q4  →  阶段二：嵌入式开发闭环
2027 Q1-Q2  →  阶段三：工控场景延伸
2027 Q3-Q4  →  阶段四：设备舰队智能化
2028+       →  阶段五：平台化 & 生态
```

---

## 阶段一：地基整固（2026 Q1-Q2）

**目标**：为后续扩展打下坚实的工程基础，消除已知技术债务。

### 1.1 测试覆盖（最高优先级）
- `security.py` 完整单元测试：正则边界、路径遍历攻击样本、`allowed_roots=[]` 语义
- `config.py` 单元测试：字段缺失、类型错误、空设备、模板格式异常
- SSH 集成测试：基于 Docker `linuxserver/openssh-server` 的真实 SSH 环境
- CI：GitHub Actions `push` → pytest，`tag` → PyPI 发布

### 1.2 安全加固
- `AutoAddPolicy` 启用时写入审计 WARNING，并在日志附加 `"host_key_policy": "auto_add"`
- 配置加载时预校验命令模板占位符数量合理性
- 添加 `connect_timeout` / `banner_timeout` 等 SSH 参数的设备级覆写支持

### 1.3 工具补全
- `file_list`：列出远端目录（受 `allowed_roots` 约束）
- `audit_tail`：查询最近 N 条审计记录（支持按工具名过滤）
- 审计日志扩展字段：`bytes_transferred`、`username`、`host_key_policy`

### 1.4 文档与发布
- `CHANGELOG.md` 建立版本条目规范
- 容器化：`Dockerfile` + `docker-compose.example.yml`
- 补充 Cursor / Windsurf / Claude Desktop 注册配置示例

---

## 阶段二：嵌入式开发闭环（2026 Q3-Q4）

**目标**：覆盖嵌入式开发"编译→部署→验证"的完整工作流，让 Agent 真正参与开发循环。

### 2.1 部署工作流工具
- `firmware_flash`：下发固件并触发 flash 命令模板（busybox dd / custom flasher）
- `service_restart`：按服务模板重启进程，支持超时检测和健康检查回调
- `deploy_and_verify`：组合工具——上传→执行→轮询状态→返回验证结果

### 2.2 日志与诊断工具
- `log_tail`：实时拉取远端日志文件的最后 N 行（受路径白名单约束）
- `log_search`：在指定日志文件中搜索关键字/正则（受约束）
- `proc_list`：列出远端进程状态（基于命令模板，不暴露任意 ps）
- `disk_usage`：查询指定路径磁盘用量

### 2.3 多设备批量操作
- `cmd_exec_batch`：对设备组并发执行同一命令模板，汇总结果
- `file_upload_batch`：向多设备分发同一文件（固件广播场景）
- 设备组（Device Group）配置支持：`groups.yaml` 中定义设备集合

### 2.4 构建产物集成
- `build_artifact_push`：从本地构建目录（CI 输出）匹配目标设备架构并上传
- 支持在命令模板中引用设备元数据变量（如 `{device.arch}`、`{device.name}`）

### 2.5 SSH 连接池
- 带 TTL 的连接缓存，减少高频工具调用时的握手开销
- 连接失败自动重试（可配置次数与退避间隔）

### 2.6 平台命令模板库（Vendor Pack）
- 建立按厂商分类的模板库：Amlogic、Rockchip、Raspberry Pi、Jetson、NXP i.MX、HiSilicon、Xilinx、OpenWrt 等
- 每个模板明确：适用芯片、典型系统、命令依赖、风险等级（只读/写配置/重启类）
- 提供模板分层：
     - 基础层：通用 Linux 可移植命令
     - 平台层：SoC 厂商专有工具（如 `vcgencmd`、`tegrastats`、`nvpmodel`）
     - 业务层：项目特定服务（如 `restart_idm`、`run_make`）
- 支持模板继承与覆盖：`platform_defaults + device_overrides`
- 目标：让新设备从“手工写命令”变为“选模板+少量覆写”

---

## 阶段三：工控场景延伸（2027 Q1-Q2）

**目标**：突破纯 SSH 边界，支持工控行业常见的接入协议和设备类型。

### 3.1 串口/UART 接入（本地）
- 新协议适配器 `serial_client.py`：基于 `pyserial`
- 工具：`serial_send`（发送命令）、`serial_read`（读取响应）
- 配置扩展：设备类型新增 `protocol: serial`，支持波特率、奇偶校验等参数
- 典型场景：MCU 调试串口、bootloader 交互、AT 指令设备

### 3.2 Modbus 读写（工控协议）
- 新适配器 `modbus_client.py`：基于 `pymodbus`，支持 TCP 和 RTU
- 工具：`modbus_read_registers`、`modbus_write_registers`
- 安全约束：寄存器地址白名单（`allowed_registers`），防止越权写入
- 典型场景：PLC 状态读取、变频器参数查询

### 3.3 HTTP/REST 设备接入
- 新适配器：基于 `httpx` 的 REST 客户端
- 命令模板扩展支持 HTTP 方法（`GET`/`POST`）+ endpoint 模板
- 适用于现代工业 IoT 网关（如带 REST API 的边缘盒子）

### 3.4 设备配置热加载
- 监听配置文件变更，无需重启网关即可生效新设备
- 适配动态设备接入场景（设备上线/下线频繁的测试台）

### 3.5 设备元数据扩展
- `DeviceConfig` 增加 `tags`、`arch`、`os_version`、`location` 等可选字段
- `device_list` 支持按 tag 过滤
- 命令模板变量可引用设备元数据

---

## 阶段四：设备舰队智能化（2027 Q3-Q4）

**目标**：从"单次操作"升级为"持续感知"，让 Agent 具备设备健康状态的上下文意识。

### 4.1 设备状态快照
- 定期执行健康检查模板并缓存结果（内存或轻量 SQLite）
- MCP 工具 `device_status`：返回缓存的最近一次健康快照 + 时间戳
- 典型用途：Agent 在操作前先查 "devkit-01 上次健康检查通过吗？"

### 4.2 异常检测（基于规则）
- 在健康检查结果中匹配预定义异常模式（正则或关键字）
- 工具 `anomaly_report`：返回当前异常设备列表及触发规则
- 配置驱动：`alert_rules` 定义检查条件和告警级别

### 4.3 操作历史与 diff
- `audit_diff`：对比同一设备两次操作间的状态变化（日志 diff、`cmd_exec` 输出 diff）
- `device_timeline`：返回指定设备的操作时间线

### 4.4 批量健康巡检
- `fleet_health_check`：对所有在线设备并发执行健康检查，汇总报告
- 支持 Markdown/JSON 两种输出格式（JSON 供 Agent 二次处理，Markdown 供人工审阅）

### 4.5 轻量 Agent 剧本（Playbook）引擎
- 在配置中定义剧本（命令序列 + 条件分支）：
  ```yaml
  playbooks:
    deploy_and_smoke_test:
      steps:
        - file_upload: {local: ./build/app.bin, remote: /tmp/app.bin}
        - cmd_exec: {key: flash_firmware, args: [/tmp/app.bin]}
        - cmd_exec: {key: smoke_test}
        - on_failure: cmd_exec {key: rollback}
  ```
- MCP 工具 `playbook_run`：执行剧本，返回每步结果
- 每步自动写入审计日志

---

## 阶段五：平台化 & 生态（2028+）

**目标**：从单一工具演变为嵌入式 AI 开发平台的核心基础设施。

### 5.1 角色与权限体系（RBAC）
- 配置中定义角色，每个角色绑定允许的工具、命令模板子集、路径白名单
- 不同 AI Agent / 用户使用不同的 API Key 关联不同角色
- 用途：让初级 Agent 只能查，让高权限 Agent 才能写入/烧录

### 5.2 远程 MCP 网关（非本地部署）
- 提供 HTTP/SSE 传输模式（当前仅 stdio）
- 边缘设备上部署网关端（轻量守护进程），AI 侧通过 HTTPS 远程调用
- 双向 mTLS 认证，替换 `AutoAddPolicy`

### 5.3 密钥管理集成
- `key_file` 支持从环境变量、HashiCorp Vault、AWS Secrets Manager 读取
- 禁止在配置文件中明文存储密码（迁移路径 + lint 警告）

### 5.4 设备注册中心
- 设备自动注册 API：设备上线时主动推送元数据
- 支持动态发现（mDNS / Zeroconf）局域网内的嵌入式设备

### 5.5 可视化审计仪表板
- 独立 Web UI（或集成到现有运维工具）
- 展示调用频率、设备状态时序、异常告警历史
- 数据源：`mcp_audit.log`（JSONL → SQLite → 查询 API）

### 5.6 插件/适配器生态
- 开放适配器接口（Adapter Protocol），允许社区贡献新协议（CAN、PROFINET、OPC-UA 等）
- 适配器发布到 PyPI，通过依赖声明按需安装
- 典型插件：`mcp-device-gateway-modbus`、`mcp-device-gateway-canbus`

---

## 长期护城河

```
安全模型（命令模板 + 路径白名单）  ←  核心差异，不可稀释
     ↓
嵌入式场景语义（设备组、固件部署、串口、Modbus）  ←  垂直壁垒
     ↓
完整审计链路（JSONL → 时间线 → diff）  ←  合规价值
     ↓
剧本引擎（操作序列化）  ←  复杂工作流加速器
     ↓
RBAC + 远程部署  ←  企业级采用门槛
```

**核心原则**：每增加一种能力，必须同时增加对应的安全约束配置项。
功能边界的扩展，永远跟着安全边界的明确定义走。

---

## 永不做的事

- 暴露任意 shell 执行接口（破坏核心安全模型）
- 成为通用 DevOps 平台（失去嵌入式垂直定位）
- 图形界面优先（保持工程师工具属性，CLI/配置驱动）
- 云端 SaaS 托管私密设备凭证（数据主权必须在用户侧）
