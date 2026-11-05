#!/bin/sh
# Wrapper: python3 -I companion/selftest.py [--sabotage]
cd "$(dirname "$0")/.." && exec python3 -I companion/selftest.py "$@"
