"""可作为现有 run_script 的 Python code 使用的有界 ZIP 解压示例；不是模型工具。

输入参数：原件相对路径、尚不存在的输出目录。失败目录带 .incomplete，不可复用。
"""

import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath


def extract(source, output, *, max_files=2000, max_bytes=128 * 1024 * 1024):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    marker = output / ".incomplete"
    marker.touch()
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        if len(members) > max_files or sum(info.file_size for info in members) > max_bytes:
            raise ValueError("archive expansion limit")
        seen = set()
        validated = []
        for info in members:
            original_name = info.orig_filename
            path = PurePosixPath(original_name)
            mode = info.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if (not path.parts or path.is_absolute() or ".." in path.parts
                    or "\\" in original_name or ":" in original_name or "\x00" in original_name
                    or kind not in (0, stat.S_IFREG, stat.S_IFDIR) or info.flag_bits & 1
                    or path.as_posix() in seen or path.parts[0] == ".incomplete"):
                raise ValueError("unsafe or unsupported archive member")
            seen.add(path.as_posix())
            validated.append((info, output.joinpath(*path.parts)))
        total = 0
        for info, target in validated:
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open("xb") as dst:
                while chunk := src.read(65536):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("archive expansion limit")
                    dst.write(chunk)
    marker.unlink()
    return {"files": len(members), "expanded_bytes": total, "output": str(output)}


if __name__ == "__main__":
    print(extract(sys.argv[1], sys.argv[2]))
