"""只供隔离器验证的无害探针；不读取或执行真实二进制。"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from agent_workbench.domain.binary.worker import restrict


def main():
    path, output = Path(sys.argv[1]), Path(sys.argv[2])
    mode = sys.argv[3]
    if mode == "sleep":
        time.sleep(5)
    elif mode == "crash":
        sys.exit(7)
    elif mode == "large":
        output.write_bytes(b"x" * 20000)
    else:
        restrict(output)
        blocked = []
        actions = {
            "network": lambda: socket.socket(),
            "process": lambda: subprocess.Popen([sys.executable, "-c", "pass"]),
            "write": lambda: path.write_bytes(b"must-not-write"),
        }
        for label, action in actions.items():
            try:
                action()
            except PermissionError:
                blocked.append(label)
        output.write_text(
            json.dumps(
                {
                    "status": "ready",
                    "blocked": blocked,
                    "secret_inherited": any(
                        k in os.environ
                        for k in (
                            "WORKBENCH_DATABASE_URL",
                            "XIAOMI_API_KEY",
                            "DASHSCOPE_API_KEY",
                            "BINARY_TEST_SECRET",
                        )
                    ),
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
