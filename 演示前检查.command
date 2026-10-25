#!/bin/zsh
cd -- "$(dirname "$0")" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  echo "请先按交付使用说明创建Python 3.12虚拟环境并安装依赖。"
  exit 1
fi
exec .venv/bin/python scripts/prepare_demo.py --start --initialize --reindex
