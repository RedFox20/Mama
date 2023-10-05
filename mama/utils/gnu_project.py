from __future__ import annotations
from typing import TYPE_CHECKING
import os
import mama.util
import mama.utils.sub_process as proc
from mama.utils.system import console

if TYPE_CHECKING:
    from mama.build_target import BuildTarget

######################################################################################

class GnuProject:
    """ 
    Helper utility class for compiling GNU projects.
    This will enable downloading, unzipping and building GNU projects.

    Example usage:
    ```
    gmp = GnuProject(target, 'gmp', '6.2.1', 'https://gmplib.org/download/gmp/{{project}}.tar.xz', 'lib/libgmp.a')
    gmp.build()
    """
    def __init__(self, target: BuildTarget, name: str, version: str, url: str, build_product: str):
        """
        - target: mama.BuildTarget building this GnuProject and collecting the build products
        - name: name of the project, eg 'gmp'
        - version: version of the project, eg '6.2.1'
        - url: url to download the project, eg 'https://gmplib.org/download/gmp/{{project}}.tar.xz'
        - build_product: the final product to build, eg 'lib/libgmp.a'
        """
        self.target = target
        self.name = name
        self.version = version
        self.name_with_version = f'{name}-{version}'
        self.url = url
        self.build_product = build_product
        self.install_dir_suffix = '-built'


    def source_dir(self):
        """ Where the project is extracted to, eg project/mips/gdb-13.2 """
        return self.target.build_dir(self.name_with_version)


    def install_dir(self):
        """ Where the project is installed to, eg project/mips/gdb-built """
        return self.target.build_dir(self.name + self.install_dir_suffix)


    def get_final_product(self):
        """ Returns the final product path, eg project/mips/gdb-built/bin/gdb """
        return self.install_dir() + "/" + self.build_product


    def should_build(self):
        """ Returns true if the final product does not exist """
        return not os.path.exists(self.get_final_product())


    def download_and_unzip(self):
        """ Downloads an unzips the project if the configure file does not exist """
        source = self.source_dir()
        configure_file = f'{source}/configure'
        if os.path.exists(configure_file):
            return # nothing to do

        build_root = self.target.build_dir()
        url = self.url.replace('{{project}}', self.name_with_version)
        local_file = mama.util.download_file(url, local_dir=build_root)

        console(f'>>> Extracting to {source}', color='green')
        os.makedirs(source, exist_ok=True)
        if local_file.endswith('.xz'):
            proc.execute_echo(build_root, f'tar -xf {local_file} -C {source} --strip-components=1')
        elif local_file.endswith('.gz') or local_file.endswith('.zip'):
            mama.util.unzip(local_file, source)

        # final check if the configure file exists
        if not os.path.exists(configure_file):
            raise Exception(f'Failed to extract to: {local_file} (no configure file at {configure_file})')


    def build(self, options='', multithreaded=False):
        """
        Downloads, Unzips and Builds the project 
            - options: additional options to pass to the configure script
            - multithreaded: if true, will use the -j option to build with multiple threads
        """
        project_dir = self.source_dir()
        makefile = f'{project_dir}/Makefile'
        try:
            self.download_and_unzip()

            console(f'>>>>>> BUILD {self.name} <<<<<<', color='green')

            # Autoconf main command is same for all of these GNU libraries
            machine = proc.execute_piped(f"{project_dir}/config.guess")
            configure = f'./configure --build={machine} --host=mipsel-elf'
            if not os.path.exists(makefile):
                console(f'>>> Configuring {self.name}', color='green')
                self.target.run_program(project_dir, f'{configure} --prefix={self.install_dir()} {options}')
                console(f'>>> Configured {self.name}', color='green')

            console(f'>>> Building {self.name}', color='green')
            jobs = f'-j {self.target.config.jobs}' if multithreaded else ''
            self.target.run_program(project_dir, f'make {jobs}')
            console(f'>>> Built {self.name}', color='green')

            console(f'>>> Installing {self.name}', color='green')
            self.target.run_program(project_dir, 'make install')
            console(f'>>> Installed {self.name}', color='green')
        except:
            console(f'>>> ERROR: Failed to build {self.name}', color='red')
            if os.path.exists(makefile):
                os.remove(makefile)
            raise

######################################################################################

