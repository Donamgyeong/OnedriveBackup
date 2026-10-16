#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
python_bin=${PYTHON_BIN:-python3.8}

if [ "$#" -eq 0 ]; then
    set -- run
fi

cd "$project_dir"
exec "$python_bin" -m onedrive_backup --config "$project_dir/config.json" "$@"
