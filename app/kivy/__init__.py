"""Kivy's server code, moved out of site/server.py one job per file (architecture programme, 2026-11-14).

Rule (kivy-deploy skill): no new code goes into server.py. New code lives here, one job per module (under ~1,500 lines,
functions under ~150); server.py imports from these modules and may only shrink. A module here never imports server.py.

seam(): tests replace server functions with `S.name = fake` (45 test files). Once a function lives in a kivy module, its
callers there hold their own reference, so the replacement would miss them. seam() makes the server module pass such a
replacement on to every kivy module bound to the same object. It acts only when someone sets an attribute on the module
from outside (tests); the running service never does, so it changes nothing in production.

Pattern store: the readers write ~950 patterns inline (re.search(r'…', text)), and Python's re module keeps only 512
compiled, so every message compiled ~1.1 of them again (fix package 4, 2026-11-15; a reviewer readers scan W1). The store is
raised to PATTERN_STORE once, here, because every kivy module is imported through this package: 989 suite phrases went
from 1,124 compiles to 0 (about 1.4 KB a pattern, so ~3 MB at most). re._MAXCACHE is CPython's own knob; on a Python
without it the line changes nothing.
"""
# How it runs: imported by site/server.py (sys.path holds the site folder); nothing here runs on its own.
import re
import sys
import types

PATTERN_STORE = 2048
if getattr(re, '_MAXCACHE', PATTERN_STORE) < PATTERN_STORE:
    re._MAXCACHE = PATTERN_STORE

_MISSING = object()


class _Seam(types.ModuleType):
    def __setattr__(self, name, value):
        old = self.__dict__.get(name, _MISSING)
        super().__setattr__(name, value)
        if old is _MISSING:
            return
        for mname, mod in list(sys.modules.items()):
            if mod is not None and mname.startswith('kivy.') and mod.__dict__.get(name, _MISSING) is old:
                mod.__dict__[name] = value


def seam(module):
    if type(module) is types.ModuleType:
        module.__class__ = _Seam
