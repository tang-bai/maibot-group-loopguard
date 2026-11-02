"""构建可安装的 MaiBot 插件包。

MaiBot WebUI 的 ZIP 安装器会逐条校验压缩包目录表（`src/webui/routers/plugin/zip_install.py`）：

- 条目名里出现反斜杠 `\\` 一律拒绝 —— 而 Windows 的 `Compress-Archive`
  写的正是反斜杠，这就是"ZIP 包含非法路径、链接或加密文件"的直接原因；
- 不允许绝对路径、`..`、控制字符、Windows 保留名、加密条目；
- 只允许一个顶层插件目录，且 `_manifest.json` 必须位于其下。

所以这里用 `zipfile` 手工写入，条目名统一用 `/`，并在打包后按同样的规则自检。

用法：

    python tools/build_package.py
    python tools/build_package.py --version 1.2.3
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "plugins" / "maibot-group-loopguard"
DIST_DIR = REPO_ROOT / "dist"

_EXCLUDED_DIR_NAMES = {"__pycache__", ".git", ".idea", ".vscode"}
_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10)}


def _collect_files(root: Path) -> list[Path]:
    """收集需要打包的文件，跳过缓存与编辑器目录。"""

    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _EXCLUDED_DIR_NAMES for part in relative.parts):
            continue
        if path.suffix.lower() in _EXCLUDED_SUFFIXES:
            continue
        files.append(path)
    return files


def _validate_entry_names(names: list[str]) -> list[str]:
    """按 MaiBot 安装器的规则回放一遍，返回发现的问题。"""

    problems: list[str] = []
    for name in names:
        if not name or "\\" in name:
            problems.append(f"{name!r}: 条目名含反斜杠或为空")
            continue
        posix = PurePosixPath(name)
        if posix.is_absolute():
            problems.append(f"{name}: 使用了绝对路径")
        for part in name.rstrip("/").split("/"):
            if part in {"", ".", ".."}:
                problems.append(f"{name}: 含非法的路径片段 {part!r}")
            elif part.endswith((".", " ")):
                problems.append(f"{name}: 片段以点或空格结尾")
            elif any(ord(char) < 32 or char in '<>:"|?*' for char in part):
                problems.append(f"{name}: 片段含非法字符")
            elif part.split(".")[0].casefold() in _WINDOWS_RESERVED:
                problems.append(f"{name}: 命中 Windows 保留名")
            elif part.casefold() in {".git", ".maibot-release.json"}:
                problems.append(f"{name}: 含不允许的元数据目录/文件")
    return problems


def build(version: str | None = None) -> Path:
    """打包并自检，返回产物路径。"""

    manifest_path = PLUGIN_DIR / "_manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"未找到 manifest：{manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    resolved_version = version or str(manifest.get("version") or "0.0.0")

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = DIST_DIR / f"maibot-group-loopguard-{resolved_version}.zip"
    if zip_path.exists():
        zip_path.unlink()

    root_name = PLUGIN_DIR.name
    entries: list[str] = []
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in _collect_files(PLUGIN_DIR):
            relative = path.relative_to(PLUGIN_DIR).as_posix()
            arcname = f"{root_name}/{relative}"
            archive.write(path, arcname)
            entries.append(arcname)

    problems = _validate_entry_names(entries)
    if problems:
        zip_path.unlink(missing_ok=True)
        raise SystemExit("打包自检失败：\n  " + "\n  ".join(problems))

    if "_manifest.json" not in {PurePosixPath(name).name for name in entries}:
        zip_path.unlink(missing_ok=True)
        raise SystemExit("打包自检失败：压缩包里没有 _manifest.json")

    depth_two = [name for name in entries if len(PurePosixPath(name).parts) == 2 and name.endswith("_manifest.json")]
    if len(depth_two) != 1:
        zip_path.unlink(missing_ok=True)
        raise SystemExit("打包自检失败：_manifest.json 必须恰好位于顶层插件目录下")

    size_kb = zip_path.stat().st_size / 1024
    print(f"已生成 {zip_path}")
    print(f"条目 {len(entries)} 个，{size_kb:.1f} KB")
    print("安装：WebUI → 插件 → 从 ZIP 安装，或解压到 <MaiBot>/plugins/ 后重载")
    return zip_path


def main() -> int:
    """命令行入口。"""

    parser = argparse.ArgumentParser(description="构建 MaiBot 插件包")
    parser.add_argument("--version", default="", help="覆盖压缩包文件名里的版本号")
    args = parser.parse_args()
    build(args.version or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
