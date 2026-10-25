#!/bin/zsh
cd "${0:A:h}"
exec .venv/bin/python -B scripts/start_local.py
