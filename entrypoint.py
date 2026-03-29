"""
PyInstaller entry point for mcp-device-gateway.
This file is NOT part of the installable package; it is only used
by build-exe.bat to produce a standalone executable.
"""
from mcp_device_gateway.server import main

if __name__ == "__main__":
    main()
