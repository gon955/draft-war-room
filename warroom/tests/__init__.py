"""App-level integration tests (SPEC 6).

Deliberately a package, for the same reason warroom/valuation/tests is: without
__init__.py, pytest's prepend import mode puts this directory on sys.path
instead of the repo root and `import warroom` fails.

The pure engine tests stay next to the engine in warroom/valuation/tests.
"""
