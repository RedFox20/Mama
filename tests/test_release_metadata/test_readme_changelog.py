"""Pins the packaging metadata a release publishes: the version, the PyPI links and the README section
that carries the newest releases onto the project page."""
import os, re
import pytest

from mama.utils.fileio import read_text_from
from testutils import REPO_ROOT

NEWEST = 3   # how many releases the README repeats, and what a reader sees without leaving PyPI


def _read(name: str) -> str:
    return read_text_from(os.path.join(REPO_ROOT, name))


@pytest.fixture(scope='module')
def releases() -> list:
    """(version, date, [entry]) for every release in changelog.txt, newest first."""
    out = []
    for block in re.split(r'\nrelease: ', '\n' + _read('changelog.txt'))[1:]:
        head, *lines = block.strip().splitlines()
        version, date = re.match(r'(\S+) \((.+)\)', head).groups()
        out.append((version, date, [l.strip(' -') for l in lines if l.strip().startswith('-')]))
    return out


@pytest.fixture(scope='module')
def section() -> str:
    """The README block PyPI shows, from the heading to the next one."""
    return _read('README.md').split('## Recent changes', 1)[1].split('\n## ', 1)[0]


def test_the_readme_repeats_the_newest_releases(releases, section):
    for version, date, entries in releases[:NEWEST]:
        assert f'**{version}** ({date})' in section, f'README misses the {version} heading'
        for entry in entries:
            assert f'- {entry}' in section, f'README misses the {version} entry: {entry}'


def test_the_readme_repeats_no_older_release(releases, section):
    for version, _, _ in releases[NEWEST:]:
        assert f'**{version}**' not in section, f'README still lists {version}, drop it'


def test_the_version_matches_the_newest_release(releases):
    assert f'__version__ = "{releases[0][0]}"' in _read('mama/_version.py')


def test_pypi_links_include_the_changelog():
    # PyPI has no field for release notes, so the sidebar link is the only pointer a version page gets
    assert '"Changelog" = "https://github.com/RedFox20/Mama/blob/master/changelog.txt"' in _read('pyproject.toml')


def test_every_changelog_line_fits_eighty_columns():
    long_lines = [l for l in _read('changelog.txt').splitlines() if len(l) > 80]
    assert not long_lines, long_lines
