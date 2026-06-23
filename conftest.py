import os
import sys

# Make the repo root importable (src/, config.py) when running pytest.
sys.path.insert(0, os.path.dirname(__file__))
