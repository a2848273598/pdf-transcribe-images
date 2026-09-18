# -*- coding: utf-8 -*-

import argparse
import hashlib
import json
import os

BUFFER_SIZE = 1024 * 1024

def sha256_file(path):
    h = hashlib.sha256()

    with open(path, "rb") as f:
        while True:
            data = f.read(BUFFER_SIZE)
            if not data:
                break
            h.update(data)

    return h.hexdigest()

def locate_file(base_dir, filename):
    direct_path = os.path.join(base_dir, filename)
    if os.path.isfile(direct_path):
        return direct_path

    for root, dirs, files in os.walk(base_dir):
        if filename in files:
            return os.path.join(root, filename)

    return None

def merge_file(manifest_path, parts_dir=None, output_path=None, force=False):
    manifest_path = os.path.abspath(manifest_path)

    if not os.path.isfile(manifest_path):
        raise FileNotFoundError(f"Manifest 不存在: {manifest_path}")

    with open(manifest_path, "r", encoding="utf-8-sig") as f:
        manifest = json.load(f)

    if manifest.get("format") != "binary-split-manifest":
        raise ValueError("不是支持的分片 Manifest")

    if manifest.get("version") != 1:
        raise ValueError(f"不支持的 Manifest 版本: {manifest.get('version')}")

    original_name = manifest["original_name"]
    original_size = int(manifest["original_size"])
    original_sha256 = manifest["original_sha256"]
    parts = manifest["parts"]

    if parts_dir is None:
        parts_dir = os.path.dirname(manifest_path)

    parts_dir = os.path.abspath(parts_dir)

    if output_path is None:
        output_path = os.path.join(parts_dir, original_name)

    output_path = os.path.abspath(output_path)

    if os.path.exists(output_path) and not force:
        raise FileExistsError(
            f"输出文件已经存在: {output_path}\n"
            f"如需覆盖，请增加 --force"
        )

    print("=" * 60)
    print("文件合并")
    print("=" * 60)
    print(f"Manifest: {manifest_path}")
    print(f"分片搜索目录: {parts_dir}")
    print(f"输出文件: {output_path}")
    print(f"分片数量: {len(parts)}")
    print(f"原文件大小: {original_size:,} 字节")
    print(f"原文件 SHA256: {original_sha256}")
    print()

    parts = sorted(parts, key=lambda item: int(item["index"]))

    expected_indexes = list(range(1, len(parts) + 1))
    actual_indexes = [int(part["index"]) for part in parts]

    if actual_indexes != expected_indexes:
        raise RuntimeError(
            "分片编号不连续，可能存在缺失或重复分片。\n"
            f"预期: {expected_indexes}\n"
            f"实际: {actual_indexes}"
        )

    resolved_parts = []

    print("开始验证所有分片...")

    for part in parts:
        part_path = locate_file(parts_dir, part["name"])

        if not part_path:
            raise FileNotFoundError(f"缺少分片: {part['name']}")

        actual_size = os.path.getsize(part_path)

        if actual_size != int(part["size"]):
            raise RuntimeError(
                f"分片大小错误: {part['name']}\n"
                f"预期: {part['size']}\n"
                f"实际: {actual_size}"
            )

        actual_hash = sha256_file(part_path)

        if actual_hash.lower() != part["sha256"].lower():
            raise RuntimeError(
                f"分片 SHA256 错误: {part['name']}\n"
                f"预期: {part['sha256']}\n"
                f"实际: {actual_hash}"
            )

        resolved_parts.append((part, part_path))
        print(f"✓ 分片 {int(part['index']):04d}: {part['name']}")

    print()
    print("✓ 所有分片完整")
    print("开始合并...")
    print()

    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    temp_path = output_path + ".merging"

    try:
        if os.path.exists(temp_path):
            os.remove(temp_path)

        merged_hash = hashlib.sha256()
        merged_size = 0

        with open(temp_path, "wb") as dst:
            for part, part_path in resolved_parts:
                print(
                    f"合并 {int(part['index']):04d}/"
                    f"{len(resolved_parts):04d}: {part['name']}"
                )

                with open(part_path, "rb") as src:
                    while True:
                        data = src.read(BUFFER_SIZE)
                        if not data:
                            break

                        dst.write(data)
                        merged_hash.update(data)
                        merged_size += len(data)

                dst.flush()

        merged_sha256 = merged_hash.hexdigest()

        print()
        print("开始验证合并文件...")
        print(f"合并大小: {merged_size:,} 字节")
        print(f"预期大小: {original_size:,} 字节")
        print(f"合并 SHA256: {merged_sha256}")
        print(f"原始 SHA256: {original_sha256}")

        if merged_size != original_size:
            raise RuntimeError("最终文件大小校验失败")

        if merged_sha256.lower() != original_sha256.lower():
            raise RuntimeError("最终文件 SHA256 校验失败")

        os.replace(temp_path, output_path)

        print()
        print("=" * 60)
        print("✓ 合并成功")
        print("✓ 合并文件与原文件逐字节完全一致")
        print("=" * 60)
        print(f"最终文件: {output_path}")
        print(f"SHA256: {merged_sha256}")

        return output_path

    except Exception:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        raise

def main():
    parser = argparse.ArgumentParser(
        description="根据 binary-split-manifest 安全合并二进制分片。"
    )

    parser.add_argument(
        "manifest",
        help="主程序生成的 *.manifest.json"
    )

    parser.add_argument(
        "--parts-dir",
        default=None,
        help="分片搜索目录；默认从 Manifest 所在目录开始递归搜索"
    )

    parser.add_argument(
        "--output",
        default=None,
        help="输出文件路径；默认恢复 Manifest 中记录的原始文件名"
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="允许覆盖已经存在的输出文件"
    )

    args = parser.parse_args()

    merge_file(
        manifest_path=args.manifest,
        parts_dir=args.parts_dir,
        output_path=args.output,
        force=args.force,
    )

if __name__ == "__main__":
    main()
