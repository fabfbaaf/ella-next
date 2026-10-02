"""MSIX file redirection must preserve the merged data view and path boundaries."""
from pathlib import Path

import pytest

from ella_runtime.modules import backup
from ella_runtime.modules.backup import BackupError, BackupService


def mapped_service(tmp_path, monkeypatch):
    logical = (tmp_path / "logical").resolve()
    alias = (tmp_path / "alias").resolve()
    original_resolve = Path.resolve
    logical.mkdir()
    alias.mkdir()
    monkeypatch.setattr(backup, "_packaged_data_alias", lambda path: alias)

    def redirected(path, *args, **kwargs):
        # Existing unredirected databases coexist with newly redirected files.
        if path != logical and path.is_relative_to(logical) and path.name != "memory.sqlite3":
            return alias / path.relative_to(logical)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected)
    return BackupService(logical), logical, alias, original_resolve


def test_msix_exact_mapping_keeps_logical_database_and_backup_paths(tmp_path, monkeypatch):
    service, logical, _, _ = mapped_service(tmp_path, monkeypatch)
    assert service.data_dir == logical
    for name in ("memory.sqlite3", "models.sqlite3", "runtime-data.lock", "backups/exports/archive.zip"):
        assert service._safe(logical / name) == logical / name
    path = logical / "backups/staged/database.sqlite3"
    assert service._safe(path, within_backups=True) == path
    assert service.root == logical / "backups"


@pytest.mark.parametrize("relative", ["other.sqlite3", "backups/exports/other.zip", "backups/work/wrong/database.sqlite3"])
def test_msix_alias_requires_the_same_relative_file_name(tmp_path, monkeypatch, relative):
    service, logical, alias, original_resolve = mapped_service(tmp_path, monkeypatch)
    target = logical / "models.sqlite3"
    monkeypatch.setattr(Path, "resolve", lambda path, *args, **kwargs: alias / relative if path == target else original_resolve(path, *args, **kwargs))
    with pytest.raises(BackupError, match="超出"):
        service._safe(target)
    with pytest.raises(BackupError, match="超出"):
        service._safe(alias / "models.sqlite3")


def test_foreign_package_cache_is_not_an_allowed_alias(tmp_path, monkeypatch):
    service, logical, _, original_resolve = mapped_service(tmp_path, monkeypatch)
    target = logical / "models.sqlite3"
    foreign = tmp_path / "foreign-package" / "models.sqlite3"
    monkeypatch.setattr(Path, "resolve", lambda path, *args, **kwargs: foreign if path == target else original_resolve(path, *args, **kwargs))
    with pytest.raises(BackupError, match="超出"):
        service._safe(target)


def test_mapping_comes_only_from_current_package_and_local_data_root(tmp_path, monkeypatch):
    local = tmp_path.resolve()
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(backup, "_package_family_name", lambda: "Known.Package_123")
    assert backup._packaged_data_alias(local / "EllaNext") == local / "Packages/Known.Package_123/LocalCache/Local/EllaNext"
    assert backup._packaged_data_alias(local.parent / "elsewhere") is None
    monkeypatch.setattr(backup, "_package_family_name", lambda: None)
    assert backup._packaged_data_alias(local / "EllaNext") is None


def test_unpackaged_child_mapping_is_observed_from_non_link_directory(tmp_path, monkeypatch):
    local = tmp_path.resolve()
    logical = local / "EllaNext"
    probe = logical / "backups"
    probe.mkdir(parents=True)
    physical = local / "Packages/Host.Package_123/LocalCache/Local/EllaNext"
    original_resolve = Path.resolve
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(backup, "_package_family_name", lambda: None)
    monkeypatch.setattr(Path, "resolve", lambda path, *args, **kwargs: physical / "backups" if path == probe else original_resolve(path, *args, **kwargs))
    assert backup._packaged_data_alias(logical) == physical
    monkeypatch.setattr(Path, "resolve", lambda path, *args, **kwargs: physical / "unrelated" if path == probe else original_resolve(path, *args, **kwargs))
    assert backup._packaged_data_alias(logical) is None


def test_reparse_directory_cannot_create_a_trusted_mapping(tmp_path, monkeypatch):
    from types import SimpleNamespace

    local = tmp_path.resolve()
    logical = local / "EllaNext"
    probe = logical / "backups"
    probe.mkdir(parents=True)
    physical = local / "Packages/Host.Package_123/LocalCache/Local/EllaNext"
    original_resolve, original_stat = Path.resolve, Path.lstat
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(backup, "_package_family_name", lambda: None)
    monkeypatch.setattr(Path, "resolve", lambda path, *args, **kwargs: physical / "backups" if path == probe else original_resolve(path, *args, **kwargs))
    monkeypatch.setattr(Path, "lstat", lambda path, *args, **kwargs: SimpleNamespace(st_mode=0o40755, st_file_attributes=0x410) if path == probe else original_stat(path, *args, **kwargs))
    assert backup._packaged_data_alias(logical) is None
