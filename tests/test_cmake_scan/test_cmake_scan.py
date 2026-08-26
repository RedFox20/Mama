"""Pins the cmake scan against example cmake text: what it reads, and what it deliberately ignores."""
import pytest

from mama.buildsys.cmake.scan import expand_cmake_dirs, has_unknown_cmake_var, scan_cmake_text


# --- the supported shapes, which are the whole contract -------------------------------------------

@pytest.mark.parametrize('written, read_as', [
    ('include(mama.cmake)',                                  ('include', 'mama.cmake')),
    ('include ( mama.cmake )',                               ('include', 'mama.cmake')),
    ('  include(mama.cmake)',                                ('include', 'mama.cmake')),
    ('INCLUDE(Mama.cmake)',                                  ('include', 'Mama.cmake')),
    ('include("mama.cmake")',                                ('include', 'mama.cmake')),
    ('include(../mama.cmake)',                               ('include', '../mama.cmake')),
    ('include(${CMAKE_CURRENT_LIST_DIR}/../mama.cmake)',     ('include', '${CMAKE_CURRENT_LIST_DIR}/../mama.cmake')),
    ('include ( ${CMAKE_SOURCE_DIR}/mama.cmake )',           ('include', '${CMAKE_SOURCE_DIR}/mama.cmake')),
    ('include("${PROJECT_SOURCE_DIR}/mama.cmake")',          ('include', '${PROJECT_SOURCE_DIR}/mama.cmake')),
    ('include("my cmake/mama.cmake")',                       ('include', 'my cmake/mama.cmake')),
    ('include("generated #1/mama.cmake")',                   ('include', 'generated #1/mama.cmake')),
    ('include("Program Files (x86)/mama.cmake")',            ('include', 'Program Files (x86)/mama.cmake')),
    ('add_subdirectory(src)',                                ('add_subdirectory', 'src')),
    ('ADD_SUBDIRECTORY(src)',                                ('add_subdirectory', 'src')),
    ('add_subdirectory("src dir")',                          ('add_subdirectory', 'src dir')),
    ('add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/src)',    ('add_subdirectory', '${CMAKE_CURRENT_SOURCE_DIR}/src')),
    ('add_subdirectory(src#names the binary dir',            ('add_subdirectory', 'src')),
    ('add_subdirectory(src$dir)',                            ('add_subdirectory', 'src$dir')),
    ('project(Foo)',                                         ('project', 'Foo')),
    ('project(Foo VERSION 1.0 LANGUAGES CXX)',               ('project', 'Foo')),
])
def test_a_supported_line_reads_as_its_command_and_first_argument(written, read_as):
    assert scan_cmake_text(written) == [read_as]


# --- the shapes mama refuses on purpose, each one a documented non-defect -------------------------

@pytest.mark.parametrize('written', [
    'include(\n    mama.cmake)',                 # the argument must end on the line the command starts
    'include( # written by mama\n  mama.cmake)',
    '# include(mama.cmake)',                     # a line comment holds no command
    'message("run mama, then include(mama.cmake)")',   # a command inside another one is text
    'set(X include(mama.cmake))',
    'if(WIN32) include(mama.cmake) endif()',     # only a command that STARTS the line matches
    'if(FALSE) include(mama.cmake)',
    '#[[ include(mama.cmake) ]]',                # a bracket comment that opens and closes on one line
    'include_directories(mama.cmake)',           # a longer command name is a different command
], ids=['split-include', 'comment-in-call', 'line-comment', 'quoted-text', 'nested-set', 'inline-if',
        'inline-if-false', 'one-line-bracket', 'longer-name'])
def test_an_unsupported_line_reads_as_no_command(written):
    # a miss ends with cmake naming mama.cmake at configure time, which is the documented trade
    assert scan_cmake_text(written) == []


@pytest.mark.parametrize('written, read_as', [
    ('add_subdirectory(\n    src)',       ('add_subdirectory', '')),
    ('include([[mama.cmake]])',           ('include', '[[mama.cmake]]')),
    ('add_subdirectory([[src]])',         ('add_subdirectory', '[[src]]')),
    ('add_subdirectory(src\\ dir)',       ('add_subdirectory', 'src\\')),
    ('add_subdirectory($(MAKEVAR)/src)',  ('add_subdirectory', '$')),
    ('add_subdirectory(src;bin)',         ('add_subdirectory', 'src;bin')),
], ids=['split-subdir', 'bracket-include', 'bracket-subdir', 'escaped-space', 'make-style', 'list'])
def test_an_unsupported_argument_reads_as_one_plain_word(written, read_as):
    # the word names a dir that does not exist, or a basename that is not mama.cmake, so the branch ends
    assert scan_cmake_text(written) == [read_as]


def test_a_bracket_comment_hides_no_line_from_the_scan():
    # the scan tracks no `#[[ ]]` state, so it reads the line and mama writes one file nothing reads
    assert scan_cmake_text('#[[\ninclude(mama.cmake)\n]]') == [('include', 'mama.cmake')]


@pytest.mark.parametrize('text, read_as', [
    ('if(FALSE)\n  project(Sub)\nendif()',                ('project', 'Sub')),
    ('function(f)\n  add_subdirectory(x)\nendfunction()', ('add_subdirectory', 'x')),
], ids=['if-false', 'function-body'])
def test_a_command_on_its_own_line_reads_even_where_cmake_skips_it(text, read_as):
    # the scan evaluates no `if()` and runs no `function()`, so it reads the line where it is written
    assert scan_cmake_text(text) == [read_as]


def test_a_hash_inside_a_quoted_argument_opens_no_comment_for_the_next_line():
    text = 'message(STATUS "build #${N}")\ninclude(mama.cmake)'
    assert scan_cmake_text(text) == [('include', 'mama.cmake')]


def test_a_whole_file_reads_in_source_order():
    text = ('cmake_minimum_required(VERSION 3.20)\n'
            'project(Root)\n'
            'include(mama.cmake)\n'
            'add_subdirectory(src)\n'
            'target_link_libraries(root PRIVATE ${MAMA_LIBS})\n')
    assert scan_cmake_text(text) == [('project', 'Root'), ('include', 'mama.cmake'),
                                     ('add_subdirectory', 'src')]


def test_a_byte_order_mark_hides_the_first_command_from_no_scan():
    assert scan_cmake_text('﻿include(mama.cmake)') == [('include', 'mama.cmake')]


# --- the four dir variables mama expands ----------------------------------------------------------

@pytest.mark.parametrize('written, expanded', [
    ('${CMAKE_CURRENT_LIST_DIR}/mama.cmake',   '/cur/mama.cmake'),
    ('${CMAKE_CURRENT_SOURCE_DIR}/mama.cmake', '/cur/mama.cmake'),
    ('${PROJECT_SOURCE_DIR}/mama.cmake',       '/proj/mama.cmake'),
    ('${CMAKE_SOURCE_DIR}/mama.cmake',         '/top/mama.cmake'),
    ('${CMAKE_SOURCE_DIR}/a/${PROJECT_SOURCE_DIR}', '/top/a//proj'),   # both, in one argument
    ('plain/mama.cmake',                       'plain/mama.cmake'),
    ('src$dir',                                'src$dir'),             # a bare `$` is content
], ids=['list-dir', 'source-dir', 'project-dir', 'top-dir', 'two-vars', 'none', 'bare-dollar'])
def test_a_known_dir_variable_expands_to_the_dir_it_names(written, expanded):
    assert expand_cmake_dirs(written, '/cur', '/proj', '/top') == expanded


def test_an_expanded_dir_that_spells_a_variable_is_not_expanded_again():
    # one pass, so a `${PROJECT_SOURCE_DIR}` inside the checkout path stays content of the name
    holds = '/tmp/${PROJECT_SOURCE_DIR}/root'
    assert expand_cmake_dirs('${CMAKE_CURRENT_LIST_DIR}/mama.cmake', holds, '/proj', '/top') == f'{holds}/mama.cmake'


@pytest.mark.parametrize('arg, unknown', [
    ('src$dir',                              False),   # a bare `$` is ordinary content
    ('${CMAKE_CURRENT_LIST_DIR}/x',          False),   # every known dir variable expands
    ('${CMAKE_SOURCE_DIR}/${PROJECT_SOURCE_DIR}', False),
    ('${FOO}/x',                             True),
    ('$ENV{HOME}/x',                         True),
    ('$CACHE{C}/x',                          True),
    ('${CMAKE_CURRENT_LIST_DIR}/${FOO}',     True),    # one unknown among the known ones still counts
], ids=['bare', 'known', 'two-known', 'brace', 'env', 'cache', 'mixed'])
def test_only_a_reference_form_names_an_unknown_variable(arg, unknown):
    assert has_unknown_cmake_var(arg) is unknown
