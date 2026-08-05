"""Compatibility surface. Every name here moved into a module under mama/utils/.

A mamafile that does `from mama.util import console` keeps working, and mama names the new home once
per name per run. Update the import and the message stops.

Why the module split happened: `mama/util.py` imported ssl, zipfile, dateutil and psutil at module
level to serve four rarely called functions, and every mama start paid about 79ms for them. The
modules group by import weight now, so a caller that wants a path helper pulls nothing costly.

Nothing binds eagerly below, not even the warning function this module calls. A binding would put the
name in globals(), __getattr__ would never run for it, and neither the warning nor the deferral would
happen. See tests/test_shim_compat/."""

import sys, types   # Python loads both before mama starts, so the deferral below still costs nothing

# name -> the module under mama/utils/ that defines it now
_MOVED = {'BuildError': 'errors', 'Color': 'system', 'GitError': 'errors', 'MAMA_SHIM_FILENAME': 'paths',
          'ProgressBar': 'progress', 'System': 'system', '_GIT_PROGRESS': 'progress', '_NON_SOURCE_ENTRIES': 'paths',
          '_OCCUPIED_SUBDIRS': 'paths', '_PERCENT_RE': 'progress', '_SKIP_DIRS': 'git_status',
          '_SKIP_PREFIX': 'git_status', '_SRC_EXTS': 'git_status', '_SRC_NAMES': 'git_status',
          '_cache_base': 'paths', '_case_key': 'git_status', '_compute_git_dir_fingerprint': 'git_status',
          '_git_fingerprints': 'git_status', '_git_output': 'git_status', '_kinds_of': 'git_status',
          '_kinds_text': 'git_status', '_log_status_check': 'git_status', '_parse_status': 'git_status',
          '_passes_filter': 'fileio', '_repo_status': 'git_status', '_repo_status_kinds': 'git_status',
          '_should_copy': 'fileio', 'back_slashes': 'paths', 'console': 'system', 'copy_dir': 'fileio',
          'copy_file': 'fileio', 'copy_files': 'fileio', 'copy_if_needed': 'fileio', 'deploy_framework': 'fileio',
          'download_and_unzip': 'net', 'download_file': 'net', 'error': 'system', 'file_sha1': 'fileio',
          'find_executable_from_system': 'fileio', 'forget_git_dir_fingerprint': 'git_status',
          'forget_repo_status': 'git_status', 'forward_slashes': 'paths', 'get_colored_text': 'system',
          'get_file_size_str': 'progress', 'get_time_str': 'progress', 'git_dir_fingerprint': 'git_status',
          'git_progress_status': 'progress', 'git_source_changed': 'git_status',
          'glob_folders_with_name_match': 'paths', 'glob_with_extensions': 'paths', 'glob_with_name_match': 'paths',
          'has_contents_changed': 'fileio', 'has_shim_marker': 'paths', 'has_source_content': 'paths',
          'has_tag_changed': 'fileio', 'is_build_input': 'git_status', 'is_dir_empty': 'paths',
          'is_file_unmodified': 'fileio', 'is_network_error': 'net', 'is_progress_line': 'progress',
          'load_repo_status': 'git_status', 'log_status_checks': 'git_status',
          'memoize_git_fingerprints': 'git_status', 'normalized_join': 'paths', 'normalized_path': 'paths',
          'parse_version': 'versions', 'path_join': 'paths', 'progress': 'system', 'read_lines_from': 'fileio',
          'read_text_from': 'fileio', 'record_source_walk': 'git_status', 'remove_tree': 'fileio',
          'save_file_if_contents_changed': 'fileio', 'short_path': 'paths', 'source_fingerprint': 'git_status',
          'source_walk_file': 'git_status', 'source_walk_moved': 'git_status', 'strstr_multi': 'paths',
          'try_unzip': 'archive', 'unzip': 'archive', 'user_cache_dir': 'paths', 'version_at_least': 'versions',
          'warning': 'system', 'write_text_to': 'fileio'}

__all__ = sorted(n for n in _MOVED if not n.startswith('_'))

_warned = set()


def __getattr__(name: str):
    """Resolve a moved name, bind it here, and name its new home once.

    PEP 562. `from mama.util import x` reaches this, and so does `mama.util.x`. The binding into
    globals() means the second lookup skips this function, so the warning cannot repeat per call."""
    module = _MOVED.get(name)
    if module is None:
        raise AttributeError(f'module mama.util has no attribute {name!r}')
    from importlib import import_module
    value = getattr(import_module(f'.utils.{module}', __package__), name)
    globals()[name] = value
    _warn_once(name, module)
    return value


def _warn_once(name: str, module: str):
    if name in _warned: return
    _warned.add(name)
    from .utils.system import warning
    warning(f'  mama.util.{name} moved to mama.utils.{module}. Update the import.')


def __dir__():
    return sorted(set(globals()) | set(_MOVED))


class _Shim(types.ModuleType):
    """Carries a write to the new home. PEP 562 gives a module a `__getattr__`, but never a
    `__setattr__`, so a plain module cannot see an assignment. mama swaps this class in below.

    Without it, a write to a moved flag lands here, the real module keeps its old value, and the caller
    gets no warning."""
    def __setattr__(self, name, value):
        module = _MOVED.get(name)
        if module:
            if name not in globals(): _warn_once(name, module)  # a read binds into globals and warns there
            from importlib import import_module
            setattr(import_module(f'.utils.{module}', __package__), name, value)
        object.__setattr__(self, name, value)


sys.modules[__name__].__class__ = _Shim
