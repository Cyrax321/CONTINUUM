import sys

print(sys.version)
try:
    import continuum

    print("continuum imported from:", continuum.__file__)
    print("version:", continuum.__version__)
except Exception as e:
    print("IMPORT FAILED:", type(e).__name__, e)
