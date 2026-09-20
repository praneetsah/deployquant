import os
import sys

ENGINE = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ENGINE)
sys.path.insert(0, os.path.join(ENGINE, "tests", "runtime"))   # conftest_helpers
