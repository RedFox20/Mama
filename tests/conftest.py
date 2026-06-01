import os
import sys

# Tests/ for `import testutils`, project root for `from mama.x import y` -
# saves every new test file from repeating the same sys.path.insert dance.
_here = os.path.dirname(__file__)
sys.path.insert(0, _here)
sys.path.insert(0, os.path.abspath(os.path.join(_here, '..')))
