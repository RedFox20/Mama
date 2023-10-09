from __future__ import annotations
from typing import TYPE_CHECKING
import os
import shlex
import shutil
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
    def __init__(self, target:BuildTarget, name:str, version:str, 
                 build_product:str,
                 url:str='',
                 git:str='',
                 autogen=False,
                 configure='configure'):
        """
        - target: mama.BuildTarget building this GnuProject and collecting the build products
        - name: name of the project, eg 'gmp'
        - version: version of the project, eg '6.2.1'
        - build_product: the final product to build, eg 'lib/libgmp.a'
        - url: url to download the project, eg 'https://gmplib.org/download/gmp/{{project}}.tar.xz'
        - git: git to clone the project from
        - autogen: whether to use ./autogen.sh before running ./configure
        - configure: the configuration command, by default 'configure' but can be 'make config' etc
        """
        self.target = target
        self.name = name
        self.version = version
        self.name_with_version = f'{name}-{version}'
        self.url = url
        self.git = git
        self.autogen = autogen
        self.build_product = build_product
        self.install_dir_suffix = '-built'
        self.make_opts = '' # default options for make
        self.host = ''
        if self.target.config.mips:
            self.host = 'mipsel-linux-gnu'
        # the configure command, by default it's 'configure'
        # however using something other than 'configure' will completely override it
        self.configure_command = configure


    def source_dir(self, subpath=''):
        """ Where the project is extracted to, eg project/mips/gdb-13.2 """
        if subpath:
            return self.target.build_dir(self.name_with_version + '/' + subpath)
        return self.target.build_dir(self.name_with_version)


    def install_dir(self, subpath=''):
        """ Where the project is installed to, eg project/mips/gdb-built """
        if subpath:
            return self.target.build_dir(self.name + self.install_dir_suffix + '/' + subpath)
        return self.target.build_dir(self.name + self.install_dir_suffix)


    def get_final_product(self):
        """ Returns the final product path, eg project/mips/gdb-built/bin/gdb """
        product = self.build_product
        if '{{source}}' in product:
            return product.replace('{{source}}', self.source_dir())
        if '{{build}}' in product:
            return product.replace('{{build}}', self.target.build_dir())
        return self.install_dir() + "/" + product


    def should_build(self):
        """ Returns true if the final product does not exist """
        return not os.path.exists(self.get_final_product())


    def get_makefile(self):
        """ Gets the Makefile, which is a build step dependency """
        return f'{self.source_dir()}/Makefile'


    def get_configure_file(self):
        """ Gets the file which performs configuration, and is thus a configuration step dependency """
        args = shlex.split(self.configure_command)
        config_cmd = args[0].strip()
        if config_cmd == 'make':  # if config_cmd is 'make', then look for a Makefile
            return self.get_makefile()
        return f'{self.source_dir()}/{config_cmd}'


    def checkout_code(self):
        """
        Checks out the code archive, either by downloading and extracting the zip archive, 
        or cloning the project from git repository.
        """
        source = self.source_dir()
        configure_file = self.get_configure_file()
        autogen_file = f'{self.source_dir()}/autogen.sh' if self.autogen else ''
        if autogen_file:
            if os.path.exists(autogen_file):
                return # nothing to do
        elif os.path.exists(configure_file):
            return # nothing to do

        build_root = self.target.build_dir()
        if self.git:
            console(f'>>> Cloning {source} from {self.git}', color='green')
            if os.system(f'git clone {self.git} {source}') != 0:
                raise Exception(f'Failed to clone {self.git} to {source}')
        else:
            url = self.url.replace('{{project}}', self.name_with_version)
            try:
                local_file = mama.util.download_file(url, local_dir=build_root)
            except Exception as e:
                raise Exception(f'Failed to download {url}: {e}')

            console(f'>>> Extracting to {source}', color='green')
            os.makedirs(source, exist_ok=True)
            if local_file.endswith('.tar.xz') \
                or local_file.endswith('.tar.gz') \
                or local_file.endswith('.tar.gz2') \
                or local_file.endswith('.tar.bz2'):
                proc.execute_echo(build_root, f'tar -xf {local_file} -C {source} --strip-components=1')
            elif local_file.endswith('.zip'):
                mama.util.unzip(local_file, source)
            else:
                console(f'>>> ERROR: Unknown archive type: {local_file}', color='red')

        # final check if the configure file exists
        if autogen_file:
            if not os.path.exists(autogen_file):
                raise Exception(f'Checkout failed, no autogen file at: {autogen_file}')
        else:
            if not os.path.exists(configure_file):
                raise Exception(f'Checkout failed, no configure file at: {configure_file}')


    def configure_env(self):
        # GNU projects need to be configured with the CC, CXX and AR environment variables set
        cc_prefix = self.target.get_cc_prefix()
        os.environ['CC'] = cc_prefix + 'gcc'
        os.environ['CXX'] = cc_prefix + 'g++'
        os.environ['AR'] = cc_prefix + 'ar'
        os.environ['LD'] = cc_prefix + 'ld'
        os.environ['READELF'] = cc_prefix + 'readelf'
        os.environ['STRIP'] = cc_prefix + 'strip'
        os.environ['RANLIB'] = cc_prefix + 'ranlib'


    def run(self, command):
        """ Runs a command in the project directory, eg 'make specialsetup' """
        self.target.run_program(self.source_dir(), command)


    def configure(self, options='', autogen_opts='', prefix=''):
        """
        Only configures the project for building by generating the Makefile
            - options: additional options to pass to the configure script
        """
        console(f'>>> Configuring {self.name}', color='green')
        self.configure_env()

        if self.autogen:
            self.run(f'./autogen.sh {autogen_opts}')

        if self.configure_command != 'configure':
            # user overrides with custom configurator, such as `make config` etc
            configure = self.configure_command
        else:
            args = ''
            if self.host:
                args += f' --host={self.host}'
            if not prefix:
                prefix = f'--prefix {self.install_dir()}'
            if not self.autogen:
                guess_machine = f'{self.source_dir()}/config.guess'
                if os.path.exists(guess_machine):
                    os.chmod(guess_machine, 0o755) # make sure it's executable
                    args += f' --build={proc.execute_piped(guess_machine)}'

            configure = f'./configure {args} {prefix}'

        self.run(f'{configure} {options}')
        console(f'>>> Configured {self.name}', color='green')


    def _get_make_opts(self, opts, multithreaded=False):
        jobs = f'-j {self.target.config.jobs}' if multithreaded else ''
        all_opts = ''
        for o in [self.make_opts, opts, jobs]:
            if all_opts: all_opts += ' '
            all_opts += o
        return all_opts


    def make(self, opts='', multithreaded=False):
        """
        Only makes the project
            - opts: extra options for make
            - multithreaded: if true, will use the -j option to build with multiple threads
        """
        make_opts = self._get_make_opts(opts, multithreaded)
        console(f'>>> Make {self.name} {make_opts}', color='green')
        self.configure_env()
        self.run(f'make {make_opts}')
        console(f'>>> Made {self.name} {make_opts}', color='green')


    def install(self, no_prefix=False):
        """ Only installs the project """
        console(f'>>> Installing {self.name}', color='green')
        self.configure_env()
        prefix = '' if no_prefix else f'PREFIX={self.install_dir()}'
        all_opts = self._get_make_opts(prefix, multithreaded=False)
        self.run(f'make {all_opts} install')
        console(f'>>> Installed {self.name}', color='green')
        return self.install_dir()


    def build(self, options='', make_opts='', prefix='',
              multithreaded=False, install=True):
        """
        Downloads, Unzips, Configures, Makes and Installs the project 
            - options: additional options to pass to the configure script
            - multithreaded: if true, will use the -j option to build with multiple threads
        """
        project_dir = self.source_dir()
        autoconf_makefile = f'{project_dir}/Makefile' if self.configure_command == 'configure' else None
        try:
            console(f'>>>>>> BUILD {self.name} <<<<<<', color='green')
            self.checkout_code()

            if not autoconf_makefile: # if not autoconf then always run the configure step
                self.configure(options=options, prefix=prefix)
            elif not os.path.exists(autoconf_makefile):
                self.configure(options=options, prefix=prefix)

            self.make(opts=make_opts, multithreaded=multithreaded)
            if install:
                self.install()
        except:
            console(f'>>> ERROR: Failed to build {self.name}', color='red')
            # with autoconf projects, delete the makefile so that it will be regenerated
            #if autoconf_makefile and os.path.exists(autoconf_makefile):
            #    os.remove(autoconf_makefile)
            raise


    def strip(self, src_path, dest_path=None):
        striptool = self.target.get_cc_prefix() + 'strip'
        out = f'-o {dest_path}' if dest_path else ''
        if os.system(f'{striptool} {src_path} {out}') != 0:
            raise Exception(f'Failed to strip {src_path}')


    def copy_file_or_link(self, src_file, dst_file):
        """ Copies a file or symlink preserving their attributes and relative symlinks """
        if os.path.islink(src_file):
            link = os.readlink(src_file)
            #console(f'link: {dst_file} -> {link}', color='yellow')
            os.remove(dst_file)
            os.symlink(link, dst_file)
        else:
            mama.util.copy_file(src_file, dst_file)


    def deploy(self, src_path=None, dest_path=None, strip=False):
        """ 
        Deploys the src_path to dest_path, 
        optionally stripping the binary in the process.
        If `src_path` is None, the get_final_product() will be used
        """
        if not src_path:
            src_path = self.get_final_product()
        if not dest_path:
            dest_path = src_path
        if strip and os.path.isfile(src_path):
            self.strip(src_path, dest_path)
        else:
            self.copy_file_or_link(src_path, dest_path)
        if not os.path.exists(dest_path):
            raise Exception(f'Failed to deploy {src_path} to {dest_path}')
        console(f'>>> Deployed {src_path} to {dest_path}', color='green')


    def deploy_dir(self, src_dir, dest_dir, strip=False):
        """ Deploys all built products from src_dir to dest_dir and optionally strips them """
        root = os.path.dirname(src_dir)
        for fulldir, _, files in os.walk(src_dir):
            for file in files:
                reldir = fulldir[len(root):].lstrip('\\/')
                if reldir:
                    dst_folder = os.path.join(dest_dir, reldir)
                else:
                    dst_folder = dest_dir
                os.makedirs(dst_folder, exist_ok=True)
                src_file = os.path.join(fulldir, file)
                dst_file = os.path.join(dst_folder, file)
                if strip and os.path.isfile(src_file):
                    self.strip(src_file, dest_path=dst_file)
                else:
                    self.copy_file_or_link(src_file, dst_file)
        console(f'>>> Deployed {src_dir} to {dest_dir}', color='green')


######################################################################################

