# MCP 服务探测与速查

本文档基于当前仓库实现和已接入实例的实时探测结果整理，适合作为 Agent 首次接入时的速查资料。

## 当前服务面

- 服务名：embedded-device-gateway
- 暴露面：17 个工具、1 个资源、1 个提示词
- 当前已加载设备：2 台，分别是“相机”和“虚拟机”
- 当前已加载模板：23 条
- 模板风险分布：low 18 条，medium 5 条，high 0 条
- 模板执行模式：sync 20 条，async 3 条
- 当前模板来源：devices_c308x.yaml

## 工具速查

### 发现与规划

- capability_overview：无参数，返回服务能力地图、工具/资源/提示词清单、推荐工作流和执行模式指引。
- device_list：无参数，返回全部设备画像列表，适合作为任何设备类操作的起点。
- device_profile_get：参数为 device_name，返回单个设备的用途、能力标签和推荐模板，不暴露连接地址和路径策略细节。
- device_ping：参数为 device_name，返回 reachable 布尔值，用于 SSH 连通性预检。
- command_template_list：无参数，返回全部模板及风险、参数、执行模式，适合发现可用命令。
- command_template_get：参数为 command_key，返回单个模板的完整元数据，适合执行前确认 exec_mode。
- task_recommend：参数为 task，返回 recommended_tool、参数草案和下一步建议，适合自然语言驱动场景。

### 命令执行

- cmd_exec：参数为 device_name、command_key、args、timeout_sec，执行单条 sync 模板并立即返回 stdout、stderr 和 exit_code。
- cmd_exec_batch：参数为 device_name、commands，按输入顺序返回多条 sync 模板的执行结果，适合并发采集指标。
- cmd_exec_async：参数为 device_name、command_key、args、timeout_sec，提交 async 模板并返回 job_id。
- cmd_exec_result：参数为 job_id，返回 running、done 或 error 状态，以及任务结果。
- custom_exec：参数为 device_name、command、timeout_sec，执行符合安全规则的自定义拼装命令。
- custom_exec_async：参数为 device_name、command、timeout_sec，异步提交符合安全规则的自定义拼装命令。

### 文件与目录

- dir_list：参数为 device_name、remote_path，返回目录项列表，包含类型、大小、修改时间和权限。
- file_stat：参数为 device_name、remote_path，返回单个远端文件或目录的元信息。
- file_upload：参数为 device_name、local_path、remote_path，将本地文件上传到白名单路径内的设备目录。
- file_download：参数为 device_name、remote_path、local_path，从设备回收文件到本地。

## 资源与提示词

- config://summary：返回当前配置摘要，包括 config_path、device_count、template_count、devices、templates。
- device_ops_prompt(task, device_name?)：生成面向 Agent 的推荐调用顺序，强调先 device_list、再 device_ping、再按 exec_mode 选择执行工具。

## 模板分类清单

### 总览

- linux_common：5 条，全部为 low 风险、sync 模式。
- device_specific.相机：15 条，其中 low 风险 12 条、medium 风险 3 条；async 1 条、sync 14 条。
- device_specific.虚拟机：3 条，其中 low 风险 1 条、medium 风险 2 条；async 2 条、sync 1 条。

### 通用 Linux 模板

- linux_common.health_check：low / sync，查看内核与运行时长。
- linux_common.disk_usage：low / sync，查看磁盘使用情况。
- linux_common.mem_usage：low / sync，查看内存使用情况。
- linux_common.list_dir：low / sync，按路径列目录。
- linux_common.read_file：low / sync，按路径读取文本文件。

### 相机设备模板

- device_specific.相机.run_idm：medium / async，停止旧进程后后台启动 IDM。
- device_specific.相机.tail_idm_log：low / sync，查看 IDM 调试日志尾部。
- device_specific.相机.stop_idm_all：medium / sync，停止 IDM 及相关脚本。
- device_specific.相机.kill_idm：medium / sync，强制停止 IDM 主进程。
- device_specific.相机.list_idm_dir：low / sync，列出 IDM 部署目录。
- device_specific.相机.idm_log_list：low / sync，列出 IDM 日志目录。
- device_specific.相机.idm_process：low / sync，检查 IDM 进程是否在运行。
- device_specific.相机.hw_cpu_serial：low / sync，读取 CPU 序列号。
- device_specific.相机.hw_emmc_cid：low / sync，读取 eMMC CID。
- device_specific.相机.hw_eth0_mac：low / sync，读取以太网 MAC。
- device_specific.相机.hw_usid：low / sync，读取 unifykeys 中的 usid。
- device_specific.相机.hw_deviceid：low / sync，读取 unifykeys 中的 deviceid。
- device_specific.相机.hw_mac_bt：low / sync，读取蓝牙 MAC。
- device_specific.相机.hw_mac_wifi：low / sync，读取 Wi-Fi MAC。
- device_specific.相机.hw_region_code：low / sync，读取区域码。

### 虚拟机模板

- device_specific.虚拟机.vm_make：medium / async，在项目目录执行指定 Make 目标。
- device_specific.虚拟机.vm_deploy：medium / async，从虚拟机将产物复制到相机。
- device_specific.虚拟机.vm_list_dir：low / sync，列出虚拟机项目目录。

## 推荐调用顺序

1. 先调用 capability_overview 或 device_list 了解当前服务面。
2. 再调用 device_profile_get 和 device_ping 确认目标设备是否适合且可达。
3. 执行前调用 command_template_get，按 exec_mode 在 cmd_exec、cmd_exec_batch、cmd_exec_async 之间选择。
4. 若模板未覆盖目标任务，可退回到 custom_exec 或 custom_exec_async。
5. 若只知道任务描述，优先使用 task_recommend，再按返回的 recommended_tool 执行。
