"""Test package.

Present so ``from tests.mcp_helpers import ...`` resolves regardless of how
pytest is invoked. Without it the import works only when the repository root
happens to land on ``sys.path``: true when running ``pytest`` from the project
directory, false in CI, which failed with ``ModuleNotFoundError: No module
named 'tests'`` on all three Python versions.
"""
