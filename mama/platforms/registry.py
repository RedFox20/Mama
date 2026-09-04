from __future__ import annotations
from .windows import Windows
from .linux import Linux
from .macos import Macos
from .ios import Ios
from .android import Android
from .raspi import Raspi
from .aarch64 import Aarch64
from .mips import Mips
from .oclea import Oclea
from .xilinx import Xilinx
from .imx8mp import Imx8mp
from ..utils.system import System


# Every platform mama supports, in CMake guard order: android is also UNIX and APPLE also matches
# Darwin, so the specific platforms come before WIN32, APPLE and UNIX. A new platform is one line here.
PLATFORMS = (Android, Windows, Ios, Macos, Raspi, Aarch64, Oclea, Xilinx, Imx8mp, Mips, Linux)


def _build_arg_map() -> dict:
    """CLI arg to (platform class, pinned arch). The arch is None unless the arg names one, so
    `raspi32` selects Raspi AND arm while `raspi` leaves the arch to the default."""
    args = {}
    for p in PLATFORMS:
        args[p.name] = (p, None)
        for alias in p.cli_aliases:
            args[alias] = (p, None)
        for alias, arch in p.arch_aliases.items():
            args[alias] = (p, arch)
    return args


ARGS = _build_arg_map()


def platform_for_arg(arg: str):
    """(platform class, pinned arch) for a CLI arg, or None when the arg names no platform."""
    return ARGS.get(arg)


def host_platform() -> type:
    """The platform mama builds for when the user names none: this machine."""
    if System.windows: return Windows
    if System.macos:   return Macos
    return Linux


def platform_named(name: str) -> type:
    """The platform class whose `name` matches. Raises when nothing matches."""
    for p in PLATFORMS:
        if p.name == name: return p
    raise KeyError(f'No platform named {name}. Known={[p.name for p in PLATFORMS]}')
