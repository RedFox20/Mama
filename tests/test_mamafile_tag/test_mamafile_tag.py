"""Pins the build reason naming a mamafile the reader can open."""
from testutils import make_mock_dep
from mama.util import short_path


def test_a_custom_mamafile_is_named_by_its_own_path(tmp_path):
    # the consumer set mamafile='mamadeps/qcoro.py', so `qcoro/mamafile.py modified` named nothing
    dep = make_mock_dep(tmp_path, name='qcoro', mamafile='mamadeps/qcoro.py')
    assert short_path(dep.mamafile_path()) == 'mamadeps/qcoro.py'


def test_a_mamafile_inside_the_clone_keeps_the_dep_name_form(tmp_path):
    assert short_path(make_mock_dep(tmp_path, name='qcoro').mamafile_path()) == 'qcoro/mamafile.py'


def test_no_mamafile_path_names_nothing():
    assert short_path(None) == ''
