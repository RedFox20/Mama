from setuptools import setup

setup(name='mama',
      version='0.1',
      description='A C++ build tool even your mama can use',
      url='https://github.com/RedFox20/Mama',
      author='Jorma Rebane',
      author_email='jorma.rebane@gmail.com',
      license='MIT',
      packages=['mama'],
      entry_points = { 'console_scripts': ['mama=mama.command_line:main'], },
      zip_safe=False)
