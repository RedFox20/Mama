# The tests here edit a working tree directly, so the fingerprint memo must stay off.
from testutils import unmemoized_git_fingerprints  # noqa: F401 - pytest picks up the autouse fixture
