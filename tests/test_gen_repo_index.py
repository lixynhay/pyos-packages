import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from tools import gen_repo_index as gen


def write_pkg(path, data=None, raw=None):
    """Write package bytes and return the exact bytes on disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_bytes(raw)
        return raw
    if data is None:
        data = {"format": 1, "name": "hello", "version": "1.0.0"}
    payload = json.dumps(data).encode("utf-8")
    path.write_bytes(payload)
    return payload


def make_pkg(path, name, version, release=1, arch="any", **extra):
    data = {
        "format": 1,
        "name": name,
        "version": version,
        "release": release,
        "arch": arch,
    }
    data.update(extra)
    return write_pkg(path, data)


def test_generates_index_with_expected_fields(tmp_path):
    packages = tmp_path / "packages"
    raw = write_pkg(
        packages / "hello-1.0.0-1-any.pkg.json",
        {
            "format": 1,
            "name": "hello",
            "version": "1.0.0",
            "release": 1,
            "arch": "any",
            "depends": ["libc>=1.0"],
            "provides": ["hello"],
            "conflicts": [],
            "description": "Hello program",
            "license": "MIT",
        },
    )
    out = tmp_path / "index.json"

    assert gen.main(
        ["--repo", "core", "--packages", str(packages), "--out", str(out)]
    ) == 0

    index = json.loads(out.read_text(encoding="utf-8"))
    assert index["format"] == 1
    assert index["repo"] == "core"
    assert isinstance(index["generated"], str)
    assert len(index["packages"]) == 1

    entry = index["packages"][0]
    assert entry["name"] == "hello"
    assert entry["version"] == "1.0.0"
    assert entry["release"] == 1
    assert entry["arch"] == "any"
    assert entry["filename"] == "hello-1.0.0-1-any.pkg.json"
    assert entry["url"] == "packages/hello-1.0.0-1-any.pkg.json"
    assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
    assert entry["size"] == len(raw)
    assert entry["depends"] == ["libc>=1.0"]
    assert entry["provides"] == ["hello"]
    assert entry["conflicts"] == []
    assert entry["description"] == "Hello program"
    assert entry["license"] == "MIT"
    assert entry["signature"] is None


def test_sha256_matches_file_bytes(tmp_path):
    packages = tmp_path / "packages"
    raw = make_pkg(
        packages / "hello.pkg.json",
        "hello",
        "1.0.0",
        description="sha check",
    )

    entry = gen.build_index(packages)["packages"][0]

    assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
    assert entry["sha256"] != hashlib.sha256(
        json.dumps(json.loads(raw), sort_keys=True).encode("utf-8")
    ).hexdigest()


def test_size_matches_file_bytes(tmp_path):
    packages = tmp_path / "packages"
    raw = make_pkg(
        packages / "hello.pkg.json",
        "hello",
        "1.0.0",
        description="size check",
    )

    entry = gen.build_index(packages)["packages"][0]

    assert entry["size"] == len(raw)


def test_duplicate_name_version_release_arch_errors(tmp_path):
    packages = tmp_path / "packages"
    data = {"format": 1, "name": "hello", "version": "1.0.0"}
    write_pkg(packages / "hello-a.pkg.json", data)
    write_pkg(packages / "hello-b.pkg.json", data)

    with pytest.raises(gen.RepoIndexError) as excinfo:
        gen.build_index(packages)

    message = str(excinfo.value)
    assert "duplicate" in message
    assert "hello-a.pkg.json" in message
    assert "hello-b.pkg.json" in message


def test_non_recursive_ignores_subdirs_with_warning(tmp_path, capsys):
    packages = tmp_path / "packages"
    make_pkg(packages / "top.pkg.json", "top", "1.0")
    make_pkg(packages / "nested" / "nested.pkg.json", "nested", "1.0")

    index = gen.build_index(packages)

    assert [entry["name"] for entry in index["packages"]] == ["top"]
    stderr = capsys.readouterr().err
    assert "warning" in stderr.lower()
    assert "nested" in stderr


def test_sorting_by_name_then_pkgvercmp_desc(tmp_path):
    packages = tmp_path / "packages"
    items = [
        ("zeta", "0.9"),
        ("alpha", "1.9"),
        ("alpha", "1.10"),
        ("alpha", "2.0"),
        ("beta", "1.0"),
    ]
    for name, version in items:
        make_pkg(
            packages / f"{name}-{version.replace(':', '_')}-1-any.pkg.json",
            name,
            version,
        )

    index = gen.build_index(packages)

    assert [(entry["name"], entry["version"]) for entry in index["packages"]] == [
        ("alpha", "2.0"),
        ("alpha", "1.10"),
        ("alpha", "1.9"),
        ("beta", "1.0"),
        ("zeta", "0.9"),
    ]


def test_epoch_and_release_sorting(tmp_path):
    packages = tmp_path / "packages"
    items = [
        ("pkg", "1.0", 1, "pkg-1.0-1.pkg.json"),
        ("pkg", "1.0", 2, "pkg-1.0-2.pkg.json"),
        ("pkg", "1:1.0", 1, "pkg-epoch1-1.pkg.json"),
        ("pkg", "1:1.0", 2, "pkg-epoch1-2.pkg.json"),
        ("pkg", "2:0.1", 1, "pkg-epoch2-1.pkg.json"),
    ]
    for name, version, release, filename in items:
        make_pkg(packages / filename, name, version, release)

    index = gen.build_index(packages)

    assert [(entry["version"], entry["release"]) for entry in index["packages"]] == [
        ("2:0.1", 1),
        ("1:1.0", 2),
        ("1:1.0", 1),
        ("1.0", 2),
        ("1.0", 1),
    ]


def test_atomic_write_replaces_existing_index(tmp_path, monkeypatch):
    out = tmp_path / "index.json"
    out.write_text('{"old": true}\n', encoding="utf-8")

    index = {
        "format": 1,
        "repo": "core",
        "generated": "2026-10-23T22:40:00Z",
        "packages": [],
    }
    original_replace = gen.os.replace
    replacements = []

    def recording_replace(source, target):
        replacements.append((source, target))
        return original_replace(source, target)

    monkeypatch.setattr(gen.os, "replace", recording_replace)
    gen.write_index_atomic(index, out)

    assert json.loads(out.read_text(encoding="utf-8")) == index
    assert replacements
    assert replacements[0][1] == out
    assert [path.name for path in tmp_path.iterdir()] == ["index.json"]


def test_invalid_json_in_packages_errors(tmp_path):
    packages = tmp_path / "packages"
    write_pkg(packages / "broken.pkg.json", raw=b'{"format": 1,')

    with pytest.raises(gen.RepoIndexError, match="invalid JSON"):
        gen.build_index(packages)


def test_unknown_format_in_package_errors(tmp_path):
    packages = tmp_path / "packages"
    write_pkg(
        packages / "hello.pkg.json",
        {"format": 2, "name": "hello", "version": "1.0.0"},
    )

    with pytest.raises(gen.RepoIndexError, match="unsupported package format"):
        gen.build_index(packages)


@pytest.mark.parametrize("field", ["name", "version"])
def test_missing_name_or_version_errors(tmp_path, field):
    packages = tmp_path / "packages"
    data = {"format": 1, "name": "hello", "version": "1.0.0"}
    del data[field]
    write_pkg(packages / "hello.pkg.json", data)

    with pytest.raises(gen.RepoIndexError, match=field):
        gen.build_index(packages)


def test_defaults_for_optional_fields(tmp_path):
    packages = tmp_path / "packages"
    write_pkg(
        packages / "hello.pkg.json",
        {"format": 1, "name": "hello", "version": "1.0.0"},
    )

    entry = gen.build_index(packages)["packages"][0]

    assert entry["release"] == 1
    assert entry["arch"] == "any"
    assert entry["depends"] == []
    assert entry["provides"] == []
    assert entry["conflicts"] == []
    assert entry["description"] == ""
    assert entry["license"] == ""
    assert entry["signature"] is None


def test_url_is_relative_packages_filename(tmp_path):
    packages = tmp_path / "packages"
    make_pkg(packages / "hello-1.0.0-1-any.pkg.json", "hello", "1.0.0")

    entry = gen.build_index(packages)["packages"][0]

    assert entry["url"] == f"packages/{entry['filename']}"
    assert not entry["url"].startswith("/")
    assert ".." not in entry["url"].split("/")


def test_index_includes_base64_package_metadata(tmp_path):
    packages = tmp_path / "packages"
    raw = make_pkg(
        packages / "blob-1.0.0-1-any.pkg.json",
        "blob",
        "1.0.0",
        description="Binary blob example package",
        files=[{
            "path": "/usr/share/pyos/examples/blob.bin",
            "content": "UFlPUy1CSU5BUlktRVhBTVBMRQD/EAo=",
            "encoding": "base64",
            "mode": "0644",
            "owner": "root",
            "group": "root",
        }],
    )

    entry = gen.build_index(packages)["packages"][0]

    assert entry["name"] == "blob"
    assert entry["version"] == "1.0.0"
    assert entry["release"] == 1
    assert entry["arch"] == "any"
    assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
    assert entry["size"] == len(raw)


def test_generated_timestamp_is_utc_iso8601(tmp_path):
    packages = tmp_path / "packages"
    make_pkg(packages / "hello.pkg.json", "hello", "1.0.0")

    generated = gen.build_index(packages)["generated"]

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", generated)
    parsed = datetime.strptime(
        generated, "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    assert abs(now - parsed) < timedelta(minutes=2)


def test_pkgvercmp_semantics():
    assert gen.pkgvercmp("1.9", "1.10") < 0
    assert gen.pkgvercmp("1.10", "1.9") > 0
    assert gen.pkgvercmp("1.0", "1.0.1") < 0
    assert gen.pkgvercmp("1.0.1", "1.0") > 0
    assert gen.pkgvercmp("1:1.0", "2:0.1") < 0
    assert gen.pkgvercmp("2:0.1", "1:1.0") > 0
    assert gen.pkgvercmp("1.0-2", "1.0-1") > 0
    # pyos treats trailing zeroes as equal.
    assert gen.pkgvercmp("1.0.0", "1.0") == 0


def test_cli_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    make_pkg(tmp_path / "packages" / "hello.pkg.json", "hello", "1.0.0")

    assert gen.main([]) == 0

    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert index["repo"] == "core"
    assert index["packages"][0]["name"] == "hello"


def test_cli_returns_nonzero_on_invalid_package(tmp_path, capsys):
    packages = tmp_path / "packages"
    write_pkg(packages / "broken.pkg.json", raw=b"not json")

    code = gen.main(["--packages", str(packages), "--out", str(tmp_path / "out.json")])

    assert code == 1
    assert "error:" in capsys.readouterr().err
