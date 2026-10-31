"""Package an explicit STT edition using only tracked, public source files."""

import argparse
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import zipfile


def package_release(repo, version, executable, output, edition="standard"):
    if edition not in {"standard", "optimized"}:
        raise ValueError("Unknown edition")
    if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}(?:\.\d+)?", version):
        raise ValueError("version must be YYYY.MM.DD[.revision]")
    repo, executable, output = Path(repo), Path(executable), Path(output)
    source = repo / edition
    names = (
        subprocess.check_output(["git", "ls-files", "-z", edition], cwd=repo)
        .decode("utf-8")
        .split("\0")
    )
    files = [Path(name) for name in names if name]
    for name in files:
        if (
            name.parts[0] != edition
            or ".." in name.parts
            or name.name == "config.txt"
            or name.suffix in (".db", ".exe", ".pyc")
            or any(
                p in ("logs", "runtime", "__pycache__", "build", "dist")
                for p in name.parts
            )
            or (repo / name).is_symlink()
        ):
            raise RuntimeError("Non-release tracked file: " + str(name))
    required = [
        "app.py",
        "settings.py",
        "tracking.py",
        "storage.py",
        "stt_new.py",
        "single_instance.py",
        "process_identity.py",
        "config.default.txt",
        "README.md",
        "screen_time_tracker_new.ico",
    ]
    if edition == "standard":
        required.append("async_logging.py")
    else:
        required += [
            "qwen_config.py",
            "docs/QWEN_SETUP.md",
            "配套资源/千问管理工具包/scripts/manager.py",
            "配套资源/千问管理工具包/service.json",
        ]
    for name in required:
        if Path(edition) / name not in files:
            raise RuntimeError("Required file is not tracked: " + name)
    suffix = "-standard" if edition == "standard" else ""
    stem = f"STT-{version}{suffix}"
    exe = output / f"stt_new-{version}{suffix}-windows-x64.exe"
    portable = output / f"{stem}-windows-x64-portable.zip"
    archive = output / f"{stem}-source.zip"
    checksums = output / f"{stem}-SHA256SUMS.txt"
    for path in (exe, portable, archive, checksums):
        if path.exists():
            raise FileExistsError(
                "Refusing to overwrite release artifact: " + str(path)
            )
    if not executable.is_file():
        raise FileNotFoundError(executable)
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(executable, exe)
    prefix = "" if edition == "standard" else f"STT-{version}/"
    with zipfile.ZipFile(portable, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(exe, prefix + "stt_new.exe")
        for name, target in [
            ("config.default.txt", "config.txt"),
            ("screen_time_tracker_new.ico", "screen_time_tracker_new.ico"),
            ("README.md", "README.md"),
        ]:
            z.write(source / name, prefix + target)
        for name in files:
            relative = name.relative_to(edition)
            if relative.parts[0] in ("docs", "配套资源"):
                z.write(repo / name, prefix + relative.as_posix())
        if edition == "optimized":
            z.write(source / "docs/QWEN_SETUP.md", prefix + "千问换机配置教程.md")
        z.write(repo / "CHANGELOG.md", prefix + "CHANGELOG.md")
        notes = repo / "docs/releases" / f"{version}.md"
        if notes.exists():
            z.write(notes, prefix + "发布说明.md")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for name in files:
            z.write(
                repo / name, stem + "-source/" + name.relative_to(edition).as_posix()
            )
    assets = [exe, portable, archive]
    checksums.write_text(
        "".join(
            hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name + "\n"
            for p in assets
        ),
        encoding="utf-8",
    )
    for path in (portable, archive):
        with zipfile.ZipFile(path) as z:
            if z.testzip() is not None:
                raise RuntimeError("ZIP verification failed")
    return [*assets, checksums]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--edition", choices=("standard", "optimized"), default="standard"
    )
    parser.add_argument("--version", required=True)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    paths = package_release(
        Path(__file__).resolve().parents[1],
        args.version,
        args.exe,
        args.out.resolve(),
        args.edition,
    )
    print("\n".join(str(p) for p in paths))


if __name__ == "__main__":
    main()
