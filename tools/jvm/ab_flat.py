"""BUGS PATH-AB-4 / PATH-AB-5: tests that pin the path finder. The two-level finder (pathing.PATH_AB / ab_for) is on for melee maps only; the tests of the
flat 32-unit A* model (route sharing PB-SHARE, long routes in pieces A-PB4, the fitted budgets of A-PB1 .. A-PB6, route shapes at ties) run with it forced off
(@flat), the same behaviours of the two-level finder have their own tests (test_path_ab.py, the *_ab variants: @on). Usage:

    from jvm.ab_flat import flat, on, variant
    @flat                     # a test method or a whole TestCase class: the switch forced off (also for melee maps)
    @on                       # ... forced on
    test_x_ab = variant(test_x, True)     # a copy of test_x that runs with the switch forced on (False: off)
"""
import functools
import unittest

from . import pathing as P


def _force(value):
    def deco(obj):
        def wrap(fn):
            @functools.wraps(fn)
            def w(*a, **k):
                old = P.PATH_AB, P.AUTO
                P.PATH_AB, P.AUTO = value, False
                try:
                    return fn(*a, **k)
                finally:
                    P.PATH_AB, P.AUTO = old
            return w
        if isinstance(obj, type):
            for name in list(vars(obj)):
                if name.startswith('test') and callable(getattr(obj, name)):
                    setattr(obj, name, wrap(getattr(obj, name)))
            if issubclass(obj, unittest.TestCase):
                su = obj.setUp
                td = obj.tearDown

                def setUp(self, _su=su):
                    self._ab_old = P.PATH_AB, P.AUTO
                    P.PATH_AB, P.AUTO = value, False
                    _su(self)

                def tearDown(self, _td=td):
                    try:
                        _td(self)
                    finally:
                        P.PATH_AB, P.AUTO = self._ab_old
                obj.setUp = setUp
                obj.tearDown = tearDown
            return obj
        return wrap(obj)
    return deco


flat = _force(False)
on = _force(True)


def variant(fn, value=True):
    """a copy of a test method that runs with the switch forced to `value` (test names: the caller picks one starting with test)"""
    return _force(value)(functools.wraps(fn)(lambda *a, **k: fn(*a, **k)))
