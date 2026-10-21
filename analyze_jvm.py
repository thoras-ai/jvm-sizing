#!/usr/bin/env python3
"""Thin entrypoint — delegates entirely to the jvm_sizing package."""
from jvm_sizing.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
