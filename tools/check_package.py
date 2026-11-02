"""按 MaiBot WebUI 安装器的规则检查插件包。

安装器的校验实现见 `src/webui/routers/plugin/zip_install.py`：条目名带反斜杠、
绝对路径、`..`、控制字符、Windows 保留名、加密位、重复路径都会被拒绝，
并且只允许一个顶层插件目录、`_manifest.json` 必须在其下。

用法：

    python tools/check_package.py dist/maibot-group-loopguard-1.0.1.zip
"""

from __future__ import annotations

import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10)}
_FORBIDDEN_CHARS = '<>:"|?*'


def check(zip_path: Path) -> tuple[bool, list[str], list[str]]:
    """检查压缩包，返回 `(是否通过, 条目名列表, 问题列表)`。"""

    problems: list[str] = []
    names: list[str] = []
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        seen: set[str] = set()
        for info in infos:
            name = info.orig_filename
            names.append(name)
            parts = name.rstrip("/").split("/")
            mode = stat.S_IFMT(info.external_attr >> 16)
            if not name:
                problems.append("存在空条目名")
                continue
            if "\\" in name:
                problems.append(f"{name}: 条目名含反斜杠（Windows 打包工具的典型问题）")
            if PurePosixPath(name).is_absolute():
                problems.append(f"{name}: 使用了绝对路径")
            for part in parts:
                if part in {"", ".", ".."}:
                    problems.append(f"{name}: 路径片段非法 {part!r}")
                elif part.endswith((".", " ")):
                    problems.append(f"{name}: 路径片段以点或空格结尾")
                elif any(ord(char) < 32 or char in _FORBIDDEN_CHARS for char in part):
                    problems.append(f"{name}: 路径片段含非法字符")
                elif part.split(".")[0].casefold() in _WINDOWS_RESERVED:
                    problems.append(f"{name}: 命中 Windows 保留名")
                elif part.casefold() in {".git", ".maibot-release.json"}:
                    problems.append(f"{name}: 含不允许的目录或文件")
            if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
                problems.append(f"{name}: 不是普通文件或目录（疑似链接）")
            if info.flag_bits & 1:
                problems.append(f"{name}: 条目被加密")
            key = str(PurePosixPath(name)).casefold()
            if key in seen:
                problems.append(f"{name}: 路径重复")
            seen.add(key)

        manifests = [
            PurePosixPath(info.orig_filename)
            for info in infos
            if not info.is_dir() and PurePosixPath(info.orig_filename).name == "_manifest.json"
        ]
        if not manifests:
            problems.append("缺少 _manifest.json")
        elif PurePosixPath("_manifest.json") not in manifests:
            roots = [item.parent for item in manifests if len(item.parts) == 2]
            if len(roots) != 1:
                problems.append("_manifest.json 必须位于根目录或唯一一层插件目录下")
            elif any(not PurePosixPath(name).is_relative_to(roots[0]) for name in names):
                problems.append("压缩包里出现了插件目录之外的条目")

    return (not problems), names, problems


def main() -> int:
    """命令行入口。"""

    if len(sys.argv) != 2:
        print("用法：python tools/check_package.py <插件包.zip>")
        return 2
    target = Path(sys.argv[1])
    if not target.exists():
        print(f"文件不存在：{target}")
        return 2

    ok, names, problems = check(target)
    for name in names:
        print(f"  {name}")
    print(f"条目 {len(names)} 个")
    if ok:
        print("PASS：符合 MaiBot ZIP 安装校验")
        return 0
    print("FAIL：")
    for problem in problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
