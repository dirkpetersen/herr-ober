#!/usr/bin/env python3
"""Setup script for herr-ober.

This setup.py exists solely to install the wrapper script to /usr/local/bin
when the package is installed as root. The main package configuration is in
pyproject.toml.
"""

import os
from setuptools import setup

# Only install the wrapper script to /usr/local/bin when running as root
data_files = []
if os.geteuid() == 0:
    data_files = [("/usr/local/bin", ["bin/ober"])]

setup(data_files=data_files)
