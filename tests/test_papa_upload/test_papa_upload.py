import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mama.papa_upload import papa_upload_to
from mama.util import normalized_path


class FakeTarget:
    def __init__(self, build_root: Path):
        build_root.mkdir(parents=True, exist_ok=True)
        self.config = SimpleNamespace(verbose=False, print=False)
        self._build_root = normalized_path(str(build_root))

    def build_dir(self, path: str = ''):
        if not path:
            return self._build_root
        return normalized_path(os.path.join(self._build_root, path))


def test_papa_upload_preserves_nested_include_prefix(tmp_path: Path):
    package_root = tmp_path / 'deploy' / 'nested_include_pkg'
    include_root = Path('include/opencv4')
    header_rel = Path('opencv2/core/mat.hpp')
    include_dir = package_root / include_root / header_rel.parent
    include_dir.mkdir(parents=True)
    (include_dir / header_rel.name).write_text('// dummy header\n')
    (package_root / 'papa.txt').write_text('P nested_include_pkg\nI include/opencv4\n')

    target = FakeTarget(tmp_path / 'linux')
    archive_name = 'nested-include-pkg-test-archive'

    with patch('mama.papa_upload.artifactory_archive_name', return_value=archive_name), \
         patch('mama.papa_upload.artifactory_upload_ftp', return_value=False):
        papa_upload_to(target, str(package_root))

    archive_path = tmp_path / 'linux' / f'{archive_name}.zip'
    assert archive_path.exists()

    with zipfile.ZipFile(archive_path) as zf:
        names = set(zf.namelist())

    expected_path = f'{include_root}/{header_rel}'
    flattened_path = f'{include_root.name}/{header_rel}'

    assert 'papa.txt' in names
    assert expected_path in names
    assert flattened_path not in names
