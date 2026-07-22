"""Pins artifactory-skip chatter: a clean/rebuild's deliberate skip is verbose-only, noart stays visible."""
from testutils import make_mock_dep


def _skips(tmp_path, **cfg):
    cfg.setdefault('verbose', False)
    dep = make_mock_dep(tmp_path, print=True, **cfg)
    dep.config.target_matches.return_value = True   # `clean all` -> every dep is a target
    lines = []
    import mama.build_dependency as bd
    orig = bd.warning
    bd.warning = lines.append
    try: dep.can_fetch_artifactory(print=True, which='LOAD')
    finally: bd.warning = orig
    return lines


def test_clean_and_rebuild_skips_are_quiet(tmp_path):
    assert _skips(tmp_path, clean=True) == []    # the CLEAN line already says it
    assert _skips(tmp_path, rebuild=True) == []


def test_noart_override_still_reports(tmp_path):
    lines = _skips(tmp_path, disable_artifactory=True)
    assert len(lines) == 1 and 'noart override' in lines[0]


def test_verbose_still_explains_the_skip(tmp_path):
    dep_lines = _skips(tmp_path, clean=True, verbose=True)
    assert len(dep_lines) == 1 and 'target clean' in dep_lines[0]
