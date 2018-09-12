import os, sys
from mama.util import normalized_path
from mama.build_dependency import BuildDependency, Git


def _get_full_path(target, path):
    if path and not os.path.isabs(path):
        if target.dep.mamafile: # if setting mamafile, then use mamafile folder:
            path = os.path.join(os.path.dirname(target.dep.mamafile), path)
        else:
            path = os.path.join(target.dep.src_dir, path)
        path = normalized_path(path)
    return path


def _get_mamafile_path(target, name, mamafile):
    if mamafile:
        return _get_full_path(target, mamafile)
    maybe_mamafile = _get_full_path(target, f'mama/{name}.py')
    if os.path.exists(maybe_mamafile):
        return maybe_mamafile
    return mamafile


def add_local(target, name, source_dir, mamafile, args):
    buildTargetClass = getattr(sys.modules['mama.build_target'], 'BuildTarget')
    src      = _get_full_path(target, source_dir)
    mamafile = _get_mamafile_path(target, name, mamafile)
    dependency = BuildDependency.get(name, target.config, buildTargetClass, \
                    workspace=target.dep.workspace, src=src, mamafile=mamafile, args=args)
    target.dep.children.append(dependency)


def add_git(target, name, git_url, git_branch, git_tag, mamafile, args):
    buildTargetClass = getattr(sys.modules['mama.build_target'], 'BuildTarget')
    git = Git(git_url, git_branch, git_tag)
    mamafile = _get_mamafile_path(target, name, mamafile)
    dependency = BuildDependency.get(name, target.config, buildTargetClass, \
                    workspace=target.dep.workspace, git=git, mamafile=mamafile, args=args)
    target.dep.children.append(dependency)

