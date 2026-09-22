# -*- coding: utf-8 -*-
import argparse
import hashlib
import json
import os
import re
import shutil

BUFFER_SIZE = 1024 * 1024

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(BUFFER_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()

def locate_file(base_dir, filename):
    direct = os.path.join(base_dir, filename)
    if os.path.isfile(direct):
        return direct
    for root, _, files in os.walk(base_dir):
        if filename in files:
            return os.path.join(root, filename)
    return None

def merge_file(manifest_path, parts_dir=None, output_path=None, force=False):
    manifest_path = os.path.abspath(manifest_path)
    with open(manifest_path, "r", encoding="utf-8-sig") as f:
        manifest = json.load(f)
    if manifest.get("format") != "binary-split-manifest" or manifest.get("version") != 1:
        raise ValueError("不支持的 Manifest")
    original_name = manifest["original_name"]
    original_size = int(manifest["original_size"])
    original_sha256 = manifest["original_sha256"]
    parts = sorted(manifest["parts"], key=lambda item: int(item["index"]))
    parts_dir = os.path.abspath(parts_dir or os.path.dirname(manifest_path))
    output_path = os.path.abspath(output_path or os.path.join(parts_dir, original_name))
    if os.path.exists(output_path) and not force:
        raise FileExistsError(f"输出文件已经存在: {output_path}；如需覆盖，请增加 --force")
    expected_indexes = list(range(1, len(parts) + 1))
    actual_indexes = [int(part["index"]) for part in parts]
    if actual_indexes != expected_indexes:
        raise RuntimeError(f"分片编号不连续: actual={actual_indexes}, expected={expected_indexes}")
    resolved = []
    for part in parts:
        part_path = locate_file(parts_dir, part["name"])
        if not part_path:
            raise FileNotFoundError(f"缺少分片: {part['name']}")
        if os.path.getsize(part_path) != int(part["size"]):
            raise RuntimeError(f"分片大小错误: {part['name']}")
        if sha256_file(part_path).lower() != part["sha256"].lower():
            raise RuntimeError(f"分片 SHA256 错误: {part['name']}")
        resolved.append(part_path)
    temp_path = output_path + ".merging"
    try:
        merged_hash = hashlib.sha256()
        merged_size = 0
        with open(temp_path, "wb") as dst:
            for part_path in resolved:
                with open(part_path, "rb") as src:
                    while True:
                        data = src.read(BUFFER_SIZE)
                        if not data:
                            break
                        dst.write(data)
                        merged_hash.update(data)
                        merged_size += len(data)
        if merged_size != original_size:
            raise RuntimeError("最终文件大小校验失败")
        if merged_hash.hexdigest().lower() != original_sha256.lower():
            raise RuntimeError("最终文件 SHA256 校验失败")
        os.replace(temp_path, output_path)
        print(f"✓ 合并成功: {output_path}")
        return output_path
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", nargs="?")
    parser.add_argument("--auto-parts", action="store_true")
    parser.add_argument("--part-token", default=None)
    parser.add_argument("--expected-size", type=int, default=None)
    parser.add_argument("--expected-sha256", default=None)
    parser.add_argument("--parts-dir", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.auto_parts:
        if not args.part_token:
            parser.error("--auto-parts 必须同时提供 --part-token")
        part_dir = os.path.abspath(args.parts_dir or ".")
        token = os.path.basename(args.part_token)
        pattern = re.compile(rf"{re.escape(token)}\.part(?P<index>\d{{4}})\.bin$", re.I)
        matches = []
        for root, _, names in os.walk(part_dir):
            for name in names:
                match = pattern.search(name)
                if match:
                    matches.append((int(match.group("index")), os.path.join(root, name)))
        matches.sort(key=lambda item: item[0])
        actual = [item[0] for item in matches]
        expected = list(range(1, len(matches) + 1))
        if not matches or actual != expected:
            raise RuntimeError(f"未找到连续分片: token={token}, actual={actual}, expected={expected}")
        output_path = os.path.abspath(args.output or f"{token}.merged")
        if os.path.exists(output_path) and not args.force:
            raise FileExistsError(f"输出文件已经存在: {output_path}；如需覆盖，请增加 --force")
        temp_path = output_path + ".merging"
        try:
            with open(temp_path, "wb") as dst:
                for _, part_path in matches:
                    with open(part_path, "rb") as src:
                        shutil.copyfileobj(src, dst, BUFFER_SIZE)
            if args.expected_size is not None and os.path.getsize(temp_path) != args.expected_size:
                raise RuntimeError("自动合并文件大小错误")
            if args.expected_sha256 is not None and sha256_file(temp_path).lower() != args.expected_sha256.lower():
                raise RuntimeError("自动合并文件 SHA256 校验失败")
            os.replace(temp_path, output_path)
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise
        print(f"✓ 自动发现并合并 {len(matches)} 个分片: {output_path}")
    else:
        if not args.manifest:
            parser.error("必须提供 manifest，或使用 --auto-parts")
        merge_file(args.manifest, args.parts_dir, args.output, args.force)

if __name__ == "__main__":
    main()
