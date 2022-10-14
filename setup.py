from setuptools import setup
import sys

if sys.version_info < (3, 6):
    raise RuntimeError("This package requires Python 3.6+")

setup(name='mama',
      version='0.6.7',
      description='A modular C++ build tool even your mama can use',
      url='https://github.com/RedFox20/Mama',
      author='Jorma Rebane',
      author_email='jorma.rebane@gmail.com',
      license='MIT',
      packages=['mama'],
      install_requires=[
            'distro',
            'keyring',
            'keyrings.cryptfile'
      ],
      entry_points = { 'console_scripts': ['mama=mama.main:main'], },
      zip_safe=False,
      python_requires='>=3.6',
      keywords=['mama', 'build', 'mamabuild', 'c', 'c++', 'tool', 'cmake', 'simple', 'easy', 'package', 'manager', 'cross-platform'],
      classifiers=[
            # How mature is this project? Common values are
            #   3 - Alpha
            #   4 - Beta
            #   5 - Production/Stable
            'Development Status :: 3 - Alpha',

            # Indicate who your project is intended for
            'Intended Audience :: Developers',
            'Topic :: Software Development :: Build Tools',

            # Pick your license as you wish (should match "license" above)
            'License :: OSI Approved :: MIT License',

            # Specify the Python versions you support here. In particular, ensure
            # that you indicate whether you support Python 2, Python 3 or both.
            'Programming Language :: Python :: 3',
            'Programming Language :: Python :: 3.6',
            'Programming Language :: Python :: 3.7',
      ],
)
