"""Replace managed source while retaining the pinned SDK's generated artifacts."""

import argparse
import hashlib
import shutil
from pathlib import Path


# Verified against the pinned CANN 9.2 SDK, including its installed operator kit.
SDK_DIRECTORIES = frozenset({"_cann_ops_custom", "include", "lib64"})
SDK_METADATA = frozenset({"_build_info.py", "_version.py"})


def shared_library(path):
    return path.name.endswith(".so") or ".so." in path.name


def sdk_names(package):
    required = SDK_DIRECTORIES | SDK_METADATA
    if any(not (package / name).exists() for name in required):
        raise ValueError("Source replacement requires the complete pinned native SDK")
    names = required | {p.name for p in package.iterdir() if shared_library(p)}
    if not any(p.name.startswith("vllm_ascend_C.") and shared_library(p) for p in package.iterdir()):
        raise ValueError("Missing pinned vllm_ascend native extension")
    for path in package.rglob("*"):
        if shared_library(path) and path.relative_to(package).parts[0] not in names:
            raise ValueError(f"Unclassified native artifact: {path.relative_to(package)}")
    return names


def native_hashes(package, names):
    result = {}
    for name in sorted(names):
        root = package / name
        paths = root.rglob("*") if root.is_dir() else (root,)
        for path in paths:
            if path.is_file() and "__pycache__" not in path.parts and path.name != ".gitkeep":
                result[path.relative_to(package)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def remove(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def install_source(source, package, examples):
    """Delete obsolete source before copying; refuse native/metadata collisions."""
    source = Path(source)
    package = Path(package)
    examples = Path(examples)
    incoming = source / "vllm_ascend"
    incoming_examples = source / "examples/disaggregated_prefill_v1"
    if package.name != "vllm_ascend" or package.is_symlink() or not (incoming / "__init__.py").is_file():
        raise ValueError("Expected explicit vllm_ascend source and package roots")
    if examples.name != "disaggregated_prefill_v1" or not incoming_examples.is_dir():
        raise ValueError("Expected explicit disaggregated example roots")
    names = sdk_names(package)
    for path in incoming.iterdir():
        if path.name in SDK_DIRECTORIES and path.is_dir():
            # Git carries only a placeholder; the generated operator kit is
            # installed here by the native SDK, not rebuilt by a source overlay.
            if all(p.is_file() and p.name == ".gitkeep" for p in path.rglob("*")):
                continue
        if path.name in names:
            raise ValueError("Source must not replace pinned native artifacts or generated metadata")
    if any(shared_library(p) for p in incoming.rglob("*")):
        raise ValueError("Source must not replace pinned native artifacts or generated metadata")
    before = native_hashes(package, names)
    for path in package.iterdir():
        if path.name not in names:
            remove(path)
    shutil.copytree(incoming, package, dirs_exist_ok=True, symlinks=True)
    if examples.exists() or examples.is_symlink():
        remove(examples)
    shutil.copytree(incoming_examples, examples, symlinks=True)
    if native_hashes(package, names) != before:
        raise RuntimeError("Pinned native SDK changed during source replacement")
    return before


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--examples", required=True, type=Path)
    parser.add_argument("--native-manifest", required=True, type=Path)
    args = parser.parse_args()
    hashes = install_source(args.source, args.package, args.examples)
    args.native_manifest.write_text("".join(f"{digest}  {args.package / path}\n" for path, digest in sorted(hashes.items())))


if __name__ == "__main__":
    main()
