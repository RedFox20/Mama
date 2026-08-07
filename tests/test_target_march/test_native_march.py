"""Pins native_march: the default an -march pin overrides. `native` only when the host IS the target."""
import pytest

from mama.platforms.platform import host_arch, native_march
from mama.utils.system import System


@pytest.fixture
def host(monkeypatch):
    """Name the arch of the build machine, so a test reads the same answer on every runner."""
    def set_host(aarch64=False, x86_64=False, x86=False):
        for flag, on in (('aarch64', aarch64), ('x86_64', x86_64), ('x86', x86)):
            monkeypatch.setattr(System, flag, on)
    return set_host


@pytest.mark.parametrize('arch, flag', [('arm64', 'aarch64'), ('x64', 'x86_64'), ('x86', 'x86')])
def test_the_host_arch_compiles_for_the_machine_it_runs_on(host, arch, flag):
    host(**{flag: True})
    assert native_march(arch) == 'native'


@pytest.mark.parametrize('arch, baseline', [('arm64', 'armv8-a'), ('x64', 'x86-64'), ('x86', 'pentium4')])
def test_a_foreign_arch_takes_the_baseline_never_native(host, arch, baseline):
    # `native` names the CPU of the build machine, so on a foreign arch it emits host instructions
    host(x86_64=(arch != 'x64'), aarch64=(arch == 'x64'))
    assert native_march(arch) == baseline


def test_an_arch_with_no_baseline_refuses_instead_of_answering_empty(host):
    # an empty -march would silently drop the flag, and the build would take the compiler default
    host(x86_64=True)
    with pytest.raises(RuntimeError, match='Unsupported arch'):
        native_march('mipsel')


@pytest.mark.parametrize('flag, arch', [('aarch64', 'arm64'), ('x86_64', 'x64'), ('x86', 'x86')])
def test_the_host_arch_answers_the_same_names_the_pin_accepts(host, flag, arch):
    host(**{flag: True})
    assert host_arch() == arch
    assert native_march(host_arch()) == 'native'   # a default build never cross-compiles by accident
