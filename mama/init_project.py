import os
from .build_dependency import BuildDependency
from .util import read_lines_from, write_text_to
from .utils.system import console
import re

def write_default_mamafile(project_name, mamafile):
    contents = f'''import mama

##
# Explore Mama docs at https://github.com/RedFox20/Mama
#
class {project_name}(mama.BuildTarget):

    # this defines where to build all the dependencies
    # for project-local workspace: workspace = 'packages'
    # for system-wide workspace: global_workspace = 'mycompany'
    workspace = 'packages'

    # grab dependencies straight from git repositories
    # if the projects are trivial or support mama, then no extra configuration is needed
    # for others you will need to supply your own mamafile
    def dependencies(self):
        self.add_git('ReCpp', 'https://github.com/RedFox20/ReCpp.git')
        #self.nothing_to_build() # if you have a header only library
        pass

    # customize CMake options in this step
    def configure(self):
        self.enable_cxx20()
        #self.add_cmake_options('BUILD_TESTS=ON', 'USE_SSE2=ON')

    ## optional: customize package exports if repository doesn't have `include` or `src`
    ##           default include and lib export works for most common static libs
    #def package(self):
    #    self.export_libs('.', ['.lib', '.a']) # export any .lib or .a from build folder
    #    self.export_includes(['include'])     # export 'include' path from source folder

    # run your custom testing steps here
    def test(self, args):
        self.gdb('bin/{project_name}', src_dir=True)

'''
    write_text_to(mamafile, contents)


def write_default_cmakelists(project_name, cmakefile):
    contents = f'''cmake_minimum_required(VERSION 3.25)
project({project_name})

# Include all mama dependencies via ${{MAMA_INCLUDES}} and ${{MAMA_LIBS}}
# For each dependency there will also be ${{SomeLibrary_LIBS}} (case-sensitive)
include(mama.cmake)
include_directories(${{MAMA_INCLUDES}})

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON) # for clang-tidy

# Executable {project_name}
include_directories("include")
file(GLOB_RECURSE PUBLIC_INTERFACE include/*.h)
file(GLOB_RECURSE PRIVATE_SOURCES  src/*.c  src/*.cpp  src/*.h)
source_group(include FILES ${{PUBLIC_INTERFACE}})
source_group(src     FILES ${{PRIVATE_SOURCES}})

add_executable({project_name} ${{PUBLIC_INTERFACE}} ${{PRIVATE_SOURCES}})
target_link_libraries({project_name} ${{MAMA_LIBS}})

install(FILES ${{PUBLIC_INTERFACE}} DESTINATION include)
install(TARGETS {project_name}
        RUNTIME DESTINATION bin
        LIBRARY DESTINATION lib)

'''
    write_text_to(cmakefile, contents)


def patch_existing_cmakelists(project_name, cmakefile):
    lines = read_lines_from(cmakefile)

    for i in range(len(lines)):
        if lines[i].startswith('include(mama.cmake)'):
            console(f'  Found include(mama.cmake) at line {i+1}. Not injecting basic mama includes.')
            return # Nothing to do!

    found_project = False
    for i in range(len(lines)):
        if bool(re.match('project\\s?\\(', lines[i], re.I)): # Match "project(" or "PROJECT (" 
            at = i+1
            lines[at:at] = [
                '\n',
                '# Include all mama dependencies via ${MAMA_INCLUDES} and ${MAMA_LIBS}\n',
                '# For each dependency there will also be ${SomeLibrary_LIBS} (case-sensitive)\n',
                'include(mama.cmake)\n',
                'include_directories(${MAMA_INCLUDES})\n',
            ]
            console(f'  Inserted include(mama.cmake) at line {at+1}.')
            found_project = True
            break
    if not found_project:
        console(f'  Could not find project() statement. Invalid CMakeLists.txt?')
        return

    inserted_link_lib = False
    for i in range(len(lines)):
        line = lines[i]
        if line.startswith('target_link_libraries('):
            if 'MAMA_LIBS' in line:
                console(f'  Already found ${{MAMA_LIBS}} at line {i+1}')
                inserted_link_lib = True
                break

            idx = line.find(')')
            if idx == -1:
                lines.insert(i+1, '    ${MAMA_LIBS}\n') # multiline target_line_libraries
                console(f'  Inserted ${{MAMA_LIBS}} at line {i+2}')
            else:
                lines[i] = line[:idx] + ' ${MAMA_LIBS}' + line[idx:] # add just before closing )
                console(f'  Inserted ${{MAMA_LIBS}} at line {i+1}')
            inserted_link_lib = True
            break

    if not inserted_link_lib:
        console(f'  Could not find suitable target_link_libraries() for ${{MAMA_LIBS}}. Please insert one manually to your CMakeLists.txt')

    contents = ''.join(lines)
    write_text_to(cmakefile, contents)


def find_cpp_main(src_dir):
    # use top-level glob in src/ to match anything with "main" in the name and .cpp extension
    for entry in os.listdir(src_dir):
        if entry.endswith('.cpp') and 'main' in entry.lower():
            return os.path.join(src_dir, entry)
    return None


def write_default_cpp_main(src_dir, project_name):
    main_cpp = os.path.join(src_dir, f'{project_name}_main.cpp')
    contents = f'''#include <rpp/debugging.h>

int main(int argc, char** argv, char** envp)
{{
    LogInfo("hello world!");
    return 0;
}}
    '''
    write_text_to(main_cpp, contents)


def mama_init_project(root: BuildDependency):
    mamafile = root.mamafile_path()
    if not os.path.exists(mamafile):
        console(f'{root.name} Creating new mamafile.py: {mamafile}')
        write_default_mamafile(root.name, root.mamafile_path())
    else:
        console(f'{root.name} Mamafile already exists: {mamafile}')

    src_dir = os.path.join(root.src_dir, 'src')
    cpp_main = find_cpp_main(src_dir)
    if cpp_main:
        console(f'{root.name} Found C++ main file: {cpp_main}')
    else:
        console(f'{root.name} No C++ main file found in src directory. Generating default main file.')
        write_default_cpp_main(src_dir, root.name)

    cmakelists = root.cmakelists_path()
    if not os.path.exists(cmakelists):
        console(f'{root.name} Creating new CMakeLists.txt: {cmakelists}')
        write_default_cmakelists(root.name, cmakelists, cpp_main)
    else:
        console(f'{root.name} Patching existing CMakeLists.txt: {cmakelists}')
        patch_existing_cmakelists(root.name, cmakelists)


