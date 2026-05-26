import sys
import os

# Make repo-root packages (e.g. examples/) importable during pytest runs.
sys.path.insert(0, os.path.dirname(__file__))
