"""Pins that a target exporting nothing publishes nothing: it marks itself, and the archive is refused."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from testutils import make_package_target

from mama.build_target import BuildTarget
from mama.papa_upload import validate_archive


CATEGORIES = ('includes', 'libs', 'syslibs', 'assets')


def _papa(**exports):
    """A PapaFileInfo stand-in carrying only what validate_archive reads before the zip."""
    return SimpleNamespace(project_name='gcsmanual', modules=exports.get('modules', []),
                           **{c: exports.get(c, []) for c in CATEGORIES})


def test_an_archive_of_only_papa_txt_is_refused(tmp_path):
    # a consumer that fetches it links nothing, and the run carries on as if the dep resolved
    with pytest.raises(RuntimeError, match='exports nothing'):
        validate_archive(str(tmp_path), _papa(), 'gcsmanual.zip')


@pytest.mark.parametrize('category', CATEGORIES)
def test_one_export_of_any_kind_is_enough_to_publish(category, tmp_path):
    # the refusal must not fire for a syslib-only or asset-only package, which are both real shapes
    one = SimpleNamespace(outpath='bin/tool') if category == 'assets' else 'something'
    papa = _papa(**{category: [one]})
    with patch('mama.papa_upload._dedupe_includes', return_value=[]), \
         patch('mama.papa_upload.zipfile.ZipFile', side_effect=AssertionError('reached the zip')):
        with pytest.raises(AssertionError, match='reached the zip'):
            validate_archive(str(tmp_path), papa, 'x.zip')


# --- the automatic mark -------------------------------------------------------

def _packaged(tmp_path, exports):
    """Run the packaging of a target whose package() hook declares `exports`, and return the target."""
    def package(self):
        for category, values in exports.items():
            getattr(self, f'exported_{category}').extend(values)
    target = make_package_target(tmp_path, package=package,
                                 dep_attrs={'from_artifactory': False, 'should_rebuild': True})
    with patch.object(BuildTarget, 'default_package_includes'), \
         patch.object(BuildTarget, 'default_package_libs'):
        target._run_packaging()
    return target


def test_a_target_that_exports_nothing_marks_itself(tmp_path):
    assert _packaged(tmp_path, {}).no_upload is True


@pytest.mark.parametrize('category', CATEGORIES)
def test_a_target_that_exports_anything_still_uploads(category, tmp_path):
    assert _packaged(tmp_path, {category: ['something']}).no_upload is False


def test_an_explicit_declaration_survives_the_packaging(tmp_path):
    # nothing_to_upload() may be set in settings(), long before any packaging runs
    def package(self): self.exported_libs.append('libfoo.a')
    target = make_package_target(tmp_path, package=package,
                                 dep_attrs={'from_artifactory': False, 'should_rebuild': True})
    target.nothing_to_upload()
    with patch.object(BuildTarget, 'default_package_includes'), \
         patch.object(BuildTarget, 'default_package_libs'):
        target._run_packaging()
    assert target.no_upload is True
