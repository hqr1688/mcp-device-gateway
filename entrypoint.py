"""PyInstaller 启动入口，仅用于 build-exe.bat 构建独立可执行文件。"""
from mcp_device_gateway.server import main

if __name__ == "__main__":
    main()
