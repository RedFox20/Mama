#!/usr/bin/env bash
# Install GCC 14 and make it the default, unless the host already has it.
# CI calls this before the gcc integration job, because a runner image may ship an older gcc.
set -euo pipefail

if command -v g++-14 >/dev/null 2>&1; then
    echo "g++-14 already installed: $(g++-14 --version | head -1)"
else
    # A blocked third-party PPA fails the whole update, and the Ubuntu archive still updated.
    sudo apt-get update || echo 'apt-get update reported an error, continuing with the lists it fetched'
    sudo apt-get install -y gcc-14 g++-14
fi

sudo update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-14 100
sudo update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-14 100
sudo update-alternatives --set gcc /usr/bin/gcc-14
sudo update-alternatives --set g++ /usr/bin/g++-14
gcc --version | head -1
g++ --version | head -1
