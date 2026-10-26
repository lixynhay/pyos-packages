#!/usr/bin/env python3
"""Generate a pyOS package repository index from text .pkg.json files.

The script only indexes already built packages. It does not import pyos and
does not build, unpack, or execute anything from the packages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from functools import cmp_to_key
from pathlib import Path


PACKAGE_FORMAT = 1
DEFAULT_REPO = "core"
DEFAULT_PACKAGES_DIR = "./packages"
DEFAULT_OUT = "./index.json"
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
VERSION_PART_RE = re.compile(r"\d+|[A-Za-z]+")


class RepoIndexError(Exception):
    """Raised for validation, duplicate, read, or write errors."""


# -- pkgvercmp (same semantics as pyos) -------------------------------------


def _version_segments(text):
    return VERSION_PART_RE.findall(str(text))


def _compare_segment(left, right):
    if left == right:
        return 0
    left_number = left.isdigit()
    right_number = right.isdigit()
    if left_number and right_number:
        left_value = int(left)
        right_value = int(right)
        return (left_value > right_value) - (left_value < right_value)
    if left_number != right_number:
        # A numeric segment is newer than an alphabetic one, as in alpm.
        return 1 if left_number else -1
    return (left > right) - (left < right)


def _compare_version_parts(left_text, right_text):
    left = _version_segments(left_text)
    right = _version_segments(right_text)
    for left_part, right_part in zip(left, right):
        if left_part == right_part:
            continue
        return _compare_segment(left_part, right_part)
    if len(left) == len(right):
        return 0
    longer = left if len(left) > len(right) else right
    rest = longer[min(len(left), len(right)):]
    # Trailing zeroes do not make a version newer: 1.0 == 1.0.0.
    if all(part.isdigit() and int(part) == 0 for part in rest):
        return 0
    return 1 if len(left) > len(right) else -1


def _split_epoch(text):
    if ":" in text:
        head, tail = text.split(":", 1)
        if head.isdigit():
            return int(head), tail
    return 0, text


def pkgvercmp(left, right):
    """Compare ``[epoch:]version[-release]`` like pyos' pkgvercmp.

    Numeric segments compare as numbers, non-numeric segments as strings;
    a missing segment is older than a present one (except trailing zeroes).
    """
    left_epoch, left_rest = _split_epoch(str(left))
    right_epoch, right_rest = _split_epoch(str(right))
    if left_epoch != right_epoch:
        return 1 if left_epoch > right_epoch else -1

    left_version, _, left_release = left_rest.partition("-")
    right_version, _, right_release = right_rest.partition("-")

    result = _compare_version_parts(left_version, right_version)
    if result:
        return result
    return _compare_version_parts(left_release, right_release)


# -- package metadata -------------------------------------------------------


def _read_json(path):
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RepoIndexError(f"{path}: cannot read file: {exc}") from exc

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RepoIndexError(f"{path}: file is not valid UTF-8: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RepoIndexError(
            f"{path}: invalid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})") from exc

    if not isinstance(data, dict):
        raise RepoIndexError(f"{path}: package must be a JSON object")
    return data, raw


def _require_string(data, field, path):
    value = data.get(field)
    if not isinstance(value, str) or not value:
        raise RepoIndexError(f"{path}: missing or invalid '{field}'")
    return value


def _optional_string(data, field, path, default=""):
    value = data.get(field, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise RepoIndexError(f"{path}: '{field}' must be a string")
    return value


def _dep_list(data, field, path):
    value = data.get(field, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value):
        raise RepoIndexError(
            f"{path}: '{field}' must be a list of strings")
    return list(value)


def _read_release(data, path):
    value = data.get("release", 1)
    if (isinstance(value, bool)
            or not (isinstance(value, int)
                    or (isinstance(value, str) and value.isdigit()))):
        raise RepoIndexError(f"{path}: invalid 'release' {value!r}")
    if isinstance(value, int) and value < 0:
        raise RepoIndexError(f"{path}: invalid 'release' {value!r}")
    return int(value)


def _validate_relative_url(url, path):
    if (url.startswith("/") or SCHEME_RE.match(url)
            or ".." in url.split("/")):
        raise RepoIndexError(
            f"{path}: generated url is not a safe relative path: {url!r}")


def _validate_name(name, path):
    if "/" in name or "\\" in name or name in (".", ".."):
        raise RepoIndexError(f"{path}: invalid package name {name!r}")


def _normalize_file_path(raw_path, path):
    """Validate an absolute package path and normalize duplicate spellings."""
    if not isinstance(raw_path, str) or not raw_path.startswith("/"):
        raise RepoIndexError(
            f"{path}: file path must be absolute: {raw_path!r}")

    parts = []
    for part in raw_path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise RepoIndexError(
                f"{path}: path traversal is not allowed: {raw_path!r}")
        parts.append(part)

    if not parts:
        raise RepoIndexError(f"{path}: invalid file path: {raw_path!r}")
    return "/" + "/".join(parts)


def _parse_mode(raw_mode, file_path, path):
    """Parse a pyOS file mode: string values are octal, numbers are decimal."""
    if isinstance(raw_mode, bool) or not isinstance(raw_mode, (int, str)):
        raise RepoIndexError(
            f"{path}: invalid mode for {file_path!r}: {raw_mode!r}")
    try:
        mode = int(raw_mode, 8) if isinstance(raw_mode, str) else raw_mode
    except ValueError as exc:
        raise RepoIndexError(
            f"{path}: invalid mode for {file_path!r}: {raw_mode!r}") from exc
    if not 0 <= mode <= 0o7777:
        raise RepoIndexError(
            f"{path}: invalid mode for {file_path!r}: {raw_mode!r}")
    return mode


def _validate_files(data, path):
    raw_files = data.get("files", [])
    if not isinstance(raw_files, list):
        raise RepoIndexError(f"{path}: 'files' must be a list")

    seen = set()
    for entry in raw_files:
        if not isinstance(entry, dict):
            raise RepoIndexError(f"{path}: file entry must be an object")

        file_path = _normalize_file_path(entry.get("path"), path)
        if file_path in seen:
            raise RepoIndexError(
                f"{path}: duplicate file path {file_path!r}")
        seen.add(file_path)

        if "encoding" in entry:
            encoding = entry["encoding"]
            if not isinstance(encoding, str):
                raise RepoIndexError(
                    f"{path}: 'encoding' must be a string")
            if encoding.lower() not in ("utf-8", "utf8", "base64"):
                raise RepoIndexError(
                    f"{path}: unsupported encoding {encoding!r} "
                    "(expected utf-8 or base64)")

        _parse_mode(entry.get("mode", "0644"), file_path, path)

        for field in ("owner", "group"):
            value = entry.get(field, "root")
            if not isinstance(value, str):
                raise RepoIndexError(
                    f"{path}: '{field}' must be a string")


def load_package(path):
    """Read and validate one .pkg.json file, returning an index entry."""
    data, raw = _read_json(path)

    package_format = data.get("format")
    if (isinstance(package_format, bool)
            or package_format != PACKAGE_FORMAT):
        raise RepoIndexError(
            f"{path}: unsupported package format {package_format!r} "
            f"(expected {PACKAGE_FORMAT})")

    name = _require_string(data, "name", path)
    _validate_name(name, path)
    version = _require_string(data, "version", path)
    release = _read_release(data, path)

    arch = data.get("arch", "any")
    if not isinstance(arch, str) or not arch:
        raise RepoIndexError(f"{path}: invalid 'arch' {arch!r}")

    _validate_files(data, path)

    filename = path.name
    url = f"packages/{filename}"
    _validate_relative_url(url, path)

    return {
        "name": name,
        "version": version,
        "release": release,
        "arch": arch,
        "filename": filename,
        "url": url,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "depends": _dep_list(data, "depends", path),
        "provides": _dep_list(data, "provides", path),
        "conflicts": _dep_list(data, "conflicts", path),
        "description": _optional_string(data, "description", path),
        "license": _optional_string(data, "license", path),
        "signature": None,
    }


def _compare_packages(left, right):
    if left["name"] != right["name"]:
        return -1 if left["name"] < right["name"] else 1

    result = pkgvercmp(right["version"], left["version"])
    if result:
        return result

    result = pkgvercmp(str(right["release"]), str(left["release"]))
    if result:
        return result

    if left["arch"] != right["arch"]:
        return -1 if left["arch"] < right["arch"] else 1

    if left["filename"] != right["filename"]:
        return -1 if left["filename"] < right["filename"] else 1
    return 0


def _utc_now():
    return (datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"))


def build_index(packages_dir, repo=DEFAULT_REPO):
    """Build the index dictionary for packages_dir."""
    packages_path = Path(packages_dir)
    if not packages_path.exists():
        raise RepoIndexError(
            f"packages directory not found: {packages_path}")
    if not packages_path.is_dir():
        raise RepoIndexError(
            f"packages path is not a directory: {packages_path}")

    try:
        children = sorted(packages_path.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise RepoIndexError(
            f"cannot list packages directory '{packages_path}': {exc}") from exc

    packages = []
    seen = {}

    for child in children:
        if child.is_dir():
            print(f"warning: skipping subdirectory: {child}", file=sys.stderr)
            continue
        if not child.is_file() or not child.name.endswith(".pkg.json"):
            continue

        entry = load_package(child)
        key = (entry["name"], entry["version"],
               entry["release"], entry["arch"])
        seen.setdefault(key, []).append(entry["filename"])
        packages.append(entry)

    duplicates = [
        (key, filenames) for key, filenames in seen.items()
        if len(filenames) > 1
    ]
    if duplicates:
        details = []
        for key, filenames in sorted(duplicates):
            details.append(
                f"  (name={key[0]!r}, version={key[1]!r}, "
                f"release={key[2]}, arch={key[3]!r}): "
                + ", ".join(sorted(filenames)))
        raise RepoIndexError(
            "duplicate package metadata:\n" + "\n".join(details))

    packages.sort(key=cmp_to_key(_compare_packages))
    return {
        "format": PACKAGE_FORMAT,
        "repo": repo,
        "generated": _utc_now(),
        "packages": packages,
    }


def write_index_atomic(index, out_path):
    """Write index.json through a temporary file and os.replace."""
    target = Path(out_path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RepoIndexError(
            f"cannot create output directory '{target.parent}': {exc}") from exc

    text = json.dumps(index, ensure_ascii=False, indent=2) + "\n"
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp",
            dir=str(target.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    except OSError as exc:
        raise RepoIndexError(f"cannot write '{target}': {exc}") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _parser():
    parser = argparse.ArgumentParser(
        description="Generate a pyOS package repository index.json.")
    parser.add_argument(
        "--repo", default=DEFAULT_REPO,
        help=f"repository name in index.json (default: {DEFAULT_REPO})")
    parser.add_argument(
        "--packages", default=DEFAULT_PACKAGES_DIR,
        help=f"directory with .pkg.json files (default: {DEFAULT_PACKAGES_DIR})")
    parser.add_argument(
        "--out", default=DEFAULT_OUT,
        help=f"output index.json path (default: {DEFAULT_OUT})")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)

    try:
        if not args.repo:
            raise RepoIndexError("--repo must not be empty")
        index = build_index(Path(args.packages), repo=args.repo)
        write_index_atomic(index, Path(args.out))
    except RepoIndexError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
