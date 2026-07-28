"""Pins the flags mama puts on the cmake configure command line."""
from testutils import make_configured_target, run_config_capturing


def test_unused_cli_variables_are_not_warned_about(tmp_path):
    """mama always passes CMAKE_C_COMPILER, so every C++-only project reports it as unused. The warning
    describes mama, not the project, so it is noise in every build log."""
    t, dep = make_configured_target(tmp_path)
    assert '--no-warn-unused-cli' in run_config_capturing(t, dep)[0]


def test_verbose_keeps_the_unused_variable_warning(tmp_path):
    # under verbose it is the only signal that an add_cmake_options() name is misspelled
    t, dep = make_configured_target(tmp_path, verbose=True)
    assert '--no-warn-unused-cli' not in run_config_capturing(t, dep)[0]
