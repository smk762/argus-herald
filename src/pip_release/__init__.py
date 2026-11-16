"""Deprecated: pip-release is now argus-herald. Import argus_herald instead; this alias goes in 0.3.

``import pip_release.platforms`` returns the same module object as ``argus_herald.platforms``, so a
plugin that subclasses ``pip_release.platforms.Publisher`` keeps working unchanged until then.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys
import warnings

import argus_herald

warnings.warn("pip_release is deprecated: import argus_herald instead.", FutureWarning, stacklevel=2)

_OLD, _NEW = "pip_release.", "argus_herald."


class _Alias(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(_OLD):
            return importlib.util.spec_from_loader(fullname, self)
        return None

    def create_module(self, spec):
        return importlib.import_module(_NEW + spec.name[len(_OLD) :])

    def exec_module(self, module):
        pass  # already executed under its new name


sys.meta_path.insert(0, _Alias())
sys.modules[__name__].__dict__.update({k: v for k, v in vars(argus_herald).items() if not k.startswith("__")})
__version__ = argus_herald.__version__
