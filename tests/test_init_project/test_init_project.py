"""Pins the mama init file-generation path."""
from unittest.mock import Mock
from mama.init_project import mama_init_project


def _root(tmp_path, name='myproj'):
    root = Mock()
    root.name = name
    root.src_dir = str(tmp_path)
    root.mamafile_path = lambda: str(tmp_path / 'mamafile.py')
    root.cmakelists_path = lambda: str(tmp_path / 'CMakeLists.txt')
    return root


def test_init_on_an_empty_dir_generates_all_three_files(tmp_path):
    mama_init_project(_root(tmp_path))
    assert (tmp_path / 'mamafile.py').exists()
    assert (tmp_path / 'CMakeLists.txt').exists()
    assert (tmp_path / 'src' / 'myproj_main.cpp').exists()


def test_init_keeps_an_existing_cpp_main(tmp_path):
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src' / 'my_main.cpp').write_text('int main() { return 0; }\n')
    mama_init_project(_root(tmp_path))
    assert not (tmp_path / 'src' / 'myproj_main.cpp').exists()


def test_generated_cmakelists_names_the_project(tmp_path):
    mama_init_project(_root(tmp_path))
    assert 'project(myproj)' in (tmp_path / 'CMakeLists.txt').read_text()
