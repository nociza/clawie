from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("clawie")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"

