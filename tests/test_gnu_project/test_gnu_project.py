"""Pins the gnu_project() helper a mamafile drives: its dirs, the path variables, and what decides
whether it builds, deploys or skips."""
import os

import pytest

from testutils import make_stub_target
from mama.platforms.linux import Linux
from mama.platforms.oclea import Oclea
from mama.utils.gnu_project import BuildProduct, GnuProject


def _target(tmp_path, platform_class=Linux, jobs=8):
    return make_stub_target(tmp_path, platform_class, jobs=jobs)


def _project(tmp_path, products=(), platform_class=Linux, jobs=8, **kw):
    return GnuProject(_target(tmp_path, platform_class, jobs), 'gmp', '6.2.1',
                      build_products=list(products), **kw)


# --- the dirs a mamafile reads back -------------------------------------------

def test_the_source_dir_carries_the_version_and_the_install_dir_does_not(tmp_path):
    # two versions extract side by side, and the install dir is what the consumer links against
    gmp = _project(tmp_path)
    assert gmp.source_dir() == gmp.target.build_dir('gmp-6.2.1')
    assert gmp.source_dir('lib') == gmp.target.build_dir('gmp-6.2.1/lib')
    assert gmp.install_dir() == gmp.target.build_dir('gmp-built')
    assert gmp.install_dir('lib') == gmp.target.build_dir('gmp-built/lib')


def test_the_makefile_and_the_configure_script_sit_in_the_source_dir(tmp_path):
    gmp = _project(tmp_path)
    assert gmp.get_makefile() == f'{gmp.source_dir()}/Makefile'


@pytest.mark.parametrize('platform_class, host', [(Linux, ''), (Oclea, 'aarch64-oclea-linux')])
def test_only_a_cross_build_names_a_host_triple(tmp_path, platform_class, host):
    # ./configure --host is what makes autotools cross-compile, and a native build must not pass one
    assert _project(tmp_path, platform_class=platform_class).host == host


# --- the path variables a build_product may use -------------------------------

@pytest.mark.parametrize('variable, expect', [
    ('{{installed}}/lib/libgmp.a', 'gmp-built/lib/libgmp.a'),
    ('{{source}}/.libs/libgmp.a', 'gmp-6.2.1/.libs/libgmp.a'),
    ('{{build}}/bin/gmp', 'bin/gmp'),
])
def test_every_path_variable_expands_to_a_real_dir(tmp_path, variable, expect):
    gmp = _project(tmp_path)
    assert gmp.get_parsed_path(variable) == f'{gmp.target.build_dir()}/{expect}'


def test_a_path_with_no_variable_is_returned_as_it_stands(tmp_path):
    assert _project(tmp_path).get_parsed_path('/opt/sdk/lib/libgmp.a') == '/opt/sdk/lib/libgmp.a'


# --- the constructor ----------------------------------------------------------

def test_one_build_product_needs_no_list(tmp_path):
    product = BuildProduct('{{installed}}/lib/libgmp.a')
    assert _project(tmp_path, products=[product]).build_products == [product]
    assert GnuProject(_target(tmp_path), 'gmp', '6.2.1', build_products=product).build_products == [product]


def test_anything_else_is_refused_instead_of_silently_ignored(tmp_path):
    with pytest.raises(RuntimeError, match='BuildProduct'):
        GnuProject(_target(tmp_path), 'gmp', '6.2.1', build_products='{{installed}}/lib/libgmp.a')


def test_a_directory_product_is_not_stripped_by_default(tmp_path):
    # strip is a binary operation, and running it over a directory of headers destroys them
    assert BuildProduct('{{installed}}/include', is_dir=True).strip is False
    assert BuildProduct('{{installed}}/lib/libgmp.a').strip is True
    assert BuildProduct('{{installed}}/include', is_dir=True, strip=True).strip is True


# --- should_build / should_deploy / has_deployables ---------------------------

def _built(tmp_path, gmp, *rel_paths):
    for rel in rel_paths:
        path = gmp.get_parsed_path(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, 'w').close()


def test_a_project_with_no_build_product_yet_builds(tmp_path):
    gmp = _project(tmp_path, [BuildProduct('{{installed}}/lib/libgmp.a')])
    assert gmp.should_build()


def test_a_project_whose_products_are_all_there_skips(tmp_path):
    gmp = _project(tmp_path, [BuildProduct('{{installed}}/lib/libgmp.a')])
    _built(tmp_path, gmp, '{{installed}}/lib/libgmp.a')
    assert not gmp.should_build()


def test_one_missing_product_out_of_several_still_builds(tmp_path):
    gmp = _project(tmp_path, [BuildProduct('{{installed}}/lib/libgmp.a'),
                              BuildProduct('{{installed}}/lib/libgmpxx.a')])
    _built(tmp_path, gmp, '{{installed}}/lib/libgmp.a')
    assert gmp.should_build()


def test_a_deployed_product_skips_the_build_even_with_no_build_dir(tmp_path):
    # a wiped build dir must not force a rebuild when the deployed copy is already in place
    deployed = str(tmp_path / 'deploy' / 'libgmp.a')
    gmp = _project(tmp_path, [BuildProduct('{{installed}}/lib/libgmp.a', deploy_path=deployed)])
    os.makedirs(os.path.dirname(deployed)); open(deployed, 'w').close()
    assert gmp.has_deployables() and not gmp.should_deploy()
    assert not gmp.should_build()


def test_a_missing_deploy_target_builds_and_deploys(tmp_path):
    gmp = _project(tmp_path, [BuildProduct('{{installed}}/lib/libgmp.a',
                                           deploy_path=str(tmp_path / 'deploy' / 'libgmp.a'))])
    assert gmp.should_deploy() and gmp.should_build()


def test_a_product_with_no_deploy_path_is_never_deployable(tmp_path):
    gmp = _project(tmp_path, [BuildProduct('{{installed}}/lib/libgmp.a')])
    assert not gmp.has_deployables() and not gmp.should_deploy()


# --- the make command line ----------------------------------------------------

def test_make_opts_join_the_projects_own_options_the_call_and_the_job_flag(tmp_path):
    gmp = _project(tmp_path, jobs=8)
    gmp.make_opts = 'CFLAGS=-O2'
    assert gmp._get_make_opts('V=1', multithreaded=True).split() == ['CFLAGS=-O2', 'V=1', '-j', '8']


def test_a_single_threaded_make_asks_for_no_job_flag(tmp_path):
    # install and configure are serial steps, and a parallel `make install` races on the same files
    assert _project(tmp_path)._get_make_opts('', multithreaded=False).split() == []
    assert _project(tmp_path)._get_make_opts('install', multithreaded=False).split() == ['install']
