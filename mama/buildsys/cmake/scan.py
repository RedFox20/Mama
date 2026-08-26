"""Reads a CMakeLists.txt to find where the `mama.cmake` proxy belongs. Every line of cmake parsing
in mama lives here, and nowhere else.

THE SCAN IS MINIMAL BY DESIGN. IT IS NOT A CMAKE PARSER, AND IT WILL NOT BECOME ONE.

It reads one physical line at a time and matches THREE commands, in any case, each with its first
argument on that same line. That is the whole contract:

    include(mama.cmake)                                 the proxy beside this CMakeLists.txt
    include("${CMAKE_CURRENT_LIST_DIR}/../mama.cmake")   the proxy through a dir variable
    add_subdirectory(src)                               the child to read next
    project(Name)                                       what PROJECT_SOURCE_DIR means below it

An argument is a quoted string, taken between the quotes, or a plain word up to the first space,
paren, quote or `#`. Four dir variables expand: CMAKE_CURRENT_LIST_DIR, CMAKE_CURRENT_SOURCE_DIR,
PROJECT_SOURCE_DIR and CMAKE_SOURCE_DIR.

DELIBERATELY NOT SUPPORTED. None of the three groups below is a defect, and a report that names one
needs no fix. `tests/test_cmake_scan/` pins every example.

Read as NO command, because only a command that STARTS its line matches:

    include(                            a call whose argument sits on the next line
        mama.cmake)
    set(X include(mama.cmake))          a command inside the arguments of another one
    if(FALSE) include(mama.cmake)       a command behind anything else on the line
    #[[ include(mama.cmake) ]]          a one-line bracket comment

Read as ONE PLAIN WORD, which then names a dir that does not exist, so that branch ends:

    add_subdirectory([[src]])           a bracket argument, brackets and all
    add_subdirectory(src\\ dir)         an escape sequence, which stops at the backslash
    add_subdirectory(src;bin)           a `;` list, which the scan does not divide
    add_subdirectory($(MAKEVAR)/src)    a make-style reference, which stops at its paren
    add_subdirectory("src ")            a name ending in a space, which normalized_path drops

Read as the command, because the scan evaluates nothing and tracks no multi-line state:

    #[[                                 a bracket comment, whose lines the scan still reads
    include(mama.cmake)
    ]]
    if(FALSE)                           a branch the scan cannot know cmake skips
        project(Sub)
    endif()
    function(f)                         a body read where written, not where a call runs it
        add_subdirectory(x)
    endfunction()

WHY MINIMAL. Both failure modes are cheap, and a wider parser is not. A miss writes no proxy, and
cmake then names `mama.cmake` as the file it wanted, at configure time, in one line. A false positive
writes one generated file that nothing reads, and `_save_mama_cmake` refuses to replace a file mama
did not generate. Twenty review rounds grew an earlier cmake lexer one spelling at a time, cost 5x
the scan time, and caught no spelling any real CMakeLists.txt writes.

Read `docs/SPEC.md`, section 7, before you widen anything here.
"""
import os, re

from ...utils.paths import normalized_join, forward_slashes
from ...utils.fileio import read_lines_from
from ...utils.system import warning


MAMA_CMAKE = 'mama.cmake'
# One command, at the start of a line, with its first argument quoted or plain. `#` ends an unquoted
# argument, and `match()` anchors at 0, so a command inside another one's arguments never matches
_COMMAND_LINE = re.compile(r'[\s﻿]*(include|add_subdirectory|project)\s*\(\s*(?:"([^"]*)"|([^"()\s#]*))', re.I)
# the cmake dir variables mama expands: the dir of the file, the nearest project(), and the top dir
_CMAKE_DIR_VAR = re.compile(r'\$\{(CMAKE_CURRENT_LIST_DIR|CMAKE_CURRENT_SOURCE_DIR|PROJECT_SOURCE_DIR|CMAKE_SOURCE_DIR)\}')
# cmake names a variable three ways. A `$` outside them is ordinary content of an argument
_CMAKE_VAR_REF = re.compile(r'\$(?:ENV|CACHE)?\{')


def has_unknown_cmake_var(arg: str) -> bool:
    """True when the argument names a variable mama does not expand, such as $ENV{} or a project one.
    It tests before substitution, because a checkout path may hold a `$` of its own."""
    return _CMAKE_VAR_REF.search(_CMAKE_DIR_VAR.sub('', arg)) is not None


def expand_cmake_dirs(arg: str, current_dir: str, project_dir: str, top_dir: str) -> str:
    """The argument with every cmake dir variable mama knows replaced by the dir it names. One pass, so
    a checkout path that spells a variable of its own stays content of the name."""
    dirs = {'CMAKE_CURRENT_LIST_DIR': current_dir, 'CMAKE_CURRENT_SOURCE_DIR': current_dir,
            'PROJECT_SOURCE_DIR': project_dir, 'CMAKE_SOURCE_DIR': top_dir}
    return _CMAKE_DIR_VAR.sub(lambda ref: dirs[ref.group(1)], arg)


def scan_cmake_text(text: str) -> list:
    """(command, first argument) for every line of `text` that starts one of the three commands.
    A substring test rejects a line before the regex runs. See the module docstring for the contract."""
    found = []
    for line in text.splitlines():
        low = line.lower()   # cmake reads a command name in any case
        if MAMA_CMAKE not in low and 'add_subdirectory' not in low and 'project' not in low:
            continue
        match = _COMMAND_LINE.match(line)
        if match: found.append((match.group(1).lower(), match.group(2) or match.group(3) or ''))
    return found


def scan_cmake_lines(cmakelists: str) -> list:
    """`scan_cmake_text` over the file at `cmakelists`, or [] when it holds none.
    'surrogateescape' keeps a byte mama cannot decode, which still has to reach the path it writes."""
    return scan_cmake_text(''.join(read_lines_from(cmakelists, errors='surrogateescape')))


def find_mama_cmake_includes(cmakelists: str, source_dir: str) -> list:
    """(dir, project_dir, argument) for every `include()` naming the `mama.cmake` proxy, in every file
    cmake reads from `cmakelists`, which `source_dir` holds. The scan follows `add_subdirectory()`, and
    an argument naming an unknown variable stops that branch. `project_dir` is the dir of the last
    `project()` ABOVE the include, which is what `PROJECT_SOURCE_DIR` expands to there."""
    pending, seen, found = [(cmakelists, source_dir, source_dir, ())], set(), []
    while pending:
        path, cwd, project_dir, ancestors = pending.pop(0)
        # cmake reads one source dir once per project scope, and two symlink aliases are two source dirs
        key = (cwd, project_dir)
        if key in seen or not os.path.exists(path): continue
        seen.add(key)
        real = os.path.realpath(cwd)
        if real in ancestors: continue   # a symlink that names an ancestor would walk that chain forever
        ancestors += (real,)
        for name, arg in scan_cmake_lines(path):
            if name == 'project':
                project_dir = cwd
            elif name == 'include':
                # the basename must match, or a write would replace a real module such as grandmama.cmake
                if os.path.basename(forward_slashes(arg)).lower() == MAMA_CMAKE:
                    found.append((cwd, project_dir, arg))
            elif arg and not has_unknown_cmake_var(arg):   # mama expands no variable a CMakeLists.txt sets
                sub = normalized_join(cwd, expand_cmake_dirs(arg, cwd, project_dir, source_dir))
                pending.append((normalized_join(sub, 'CMakeLists.txt'), sub, project_dir, ancestors))
    return found


def proxy_paths(cmakelists: str, cmake_dir: str, src_dir: str, name: str) -> list:
    """Every path a proxy `include()` names, resolved against the dir of the file that names it. A path
    that leaves both `src_dir` and `cmake_dir` gets a warning and no entry. `name` names the dep in it."""
    # realpath, because a symlink inside the source dir leads out of it, and a plain prefix test misses that
    roots = tuple(os.path.realpath(d) + os.sep for d in (src_dir, cmake_dir))
    paths = []
    for source_dir, project_dir, arg in find_mama_cmake_includes(cmakelists, cmake_dir):
        # a variable mama does not expand, such as $ENV{}, means the default answers
        unknown = has_unknown_cmake_var(arg)
        path = normalized_join(source_dir, MAMA_CMAKE if unknown else
                               expand_cmake_dirs(arg, source_dir, project_dir, cmake_dir))
        if not os.path.realpath(path).startswith(roots):
            warning(f'{name}: mama writes no proxy outside its source dir: include({arg})')
        elif path not in paths:
            paths.append(path)
    return paths
