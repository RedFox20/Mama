import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mama.papa_deploy import PapaFileInfo
from mama.papa_upload import papa_upload_to, validate_archive
from mama.util import normalized_path


class FakeTarget:
    def __init__(self, build_root: Path):
        build_root.mkdir(parents=True, exist_ok=True)
        self.name = 'sample_pkg'
        self.config = SimpleNamespace(verbose=False, print=False)
        self._build_root = normalized_path(str(build_root))

    def build_dir(self, path: str = ''):
        if not path:
            return self._build_root
        return normalized_path(os.path.join(self._build_root, path))


def create_sample_package(tmp_path: Path):
    package_root = tmp_path / 'deploy' / 'sample_pkg'
    files = {
        'include/public_headers/sample/header.hpp': '// dummy header\n',
        'include/version.h': '#define SAMPLE_VERSION 1\n',
        'lib/libsample.a': 'archive\n',
        'bin/tool': 'tool\n',
    }

    for relpath, content in files.items():
        full_path = package_root / relpath
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)

    (package_root / 'papa.txt').write_text(
        'P sample_pkg\n'
        'I include/public_headers\n'
        'I include/version.h\n'
        'L lib/libsample.a\n'
        'A bin/tool\n'
    )
    return package_root, PapaFileInfo(str(package_root / 'papa.txt'))


def build_archive(archive_path: Path, entries: dict[str, Path]):
    with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for archive_name, source in entries.items():
            zf.write(source, archive_name)


def expected_archive_entries(package_root: Path):
    return {
        'papa.txt': package_root / 'papa.txt',
        'include/public_headers/sample/header.hpp': package_root / 'include/public_headers/sample/header.hpp',
        'include/version.h': package_root / 'include/version.h',
        'lib/libsample.a': package_root / 'lib/libsample.a',
        'bin/tool': package_root / 'bin/tool',
    }


def test_papa_upload_preserves_declared_paths_for_supported_content_types(tmp_path: Path):
    package_root, _ = create_sample_package(tmp_path)
    target = FakeTarget(tmp_path / 'linux')
    archive_name = 'sample-pkg-test-archive'

    with patch('mama.papa_upload.artifactory_archive_name', return_value=archive_name), \
         patch('mama.papa_upload.artifactory_upload_ftp', return_value=False):
        papa_upload_to(target, str(package_root))

    archive_path = tmp_path / 'linux' / f'{archive_name}.zip'
    assert archive_path.exists()

    with zipfile.ZipFile(archive_path) as zf:
        names = {
            info.filename
            for info in zf.infolist()
            if not info.is_dir()
        }

    assert names == set(expected_archive_entries(package_root))


def test_validate_archive_rejects_missing_content(tmp_path: Path):
    package_root, papa = create_sample_package(tmp_path / 'missing')
    entries = expected_archive_entries(package_root)
    entries.pop('lib/libsample.a')
    archive_path = tmp_path / 'missing.zip'
    build_archive(archive_path, entries)

    with pytest.raises(RuntimeError, match='missing='):
        validate_archive(str(package_root), papa, str(archive_path))


def test_validate_archive_rejects_unexpected_content(tmp_path: Path):
    package_root, papa = create_sample_package(tmp_path / 'unexpected')
    entries = expected_archive_entries(package_root)
    entries['share/extra.txt'] = package_root / 'bin/tool'
    archive_path = tmp_path / 'unexpected.zip'
    build_archive(archive_path, entries)

    with pytest.raises(RuntimeError, match='unexpected='):
        validate_archive(str(package_root), papa, str(archive_path))
