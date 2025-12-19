#!/usr/bin/env python3
"""Herr Ober - High-performance S3 ingress controller for Ceph RGW clusters."""

__version__ = "0.1.16"
__author__ = "Dirk Petersen"

from ober.config import OberConfig
from ober.system import SystemInfo

__all__ = ["__version__", "OberConfig", "SystemInfo"]
