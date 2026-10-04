#!/usr/bin/env bash
# Build dist/tcg-oracle-searcher-lambda.zip for the Lambda python3.13 arm64 runtime.
# Contents: oracle_searcher/, api/__init__.py, api/card_processing.py, api/parsing/ (no tests) and the
# runtime dependencies (requirements/lambda.txt) as manylinux aarch64 wheels.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT=dist/tcg-oracle-searcher-lambda.zip
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

"${PYTHON:-python3}" -m pip install --quiet --no-compile --requirement requirements/lambda.txt \
  --platform manylinux2014_aarch64 --only-binary=:all: --python-version 3.13 --implementation cp \
  --target "$STAGE"

cp -r oracle_searcher "$STAGE/"
mkdir -p "$STAGE/api"
cp api/__init__.py api/card_processing.py "$STAGE/api/"
cp -r api/parsing "$STAGE/api/"
find "$STAGE" -name __pycache__ -type d -prune -exec rm -rf {} +
rm -rf "$STAGE/api/parsing/tests"
find "$STAGE" -name '*.dist-info' -type d -prune -exec rm -rf {} +
rm -rf "$STAGE/bin"

mkdir -p dist
rm -f "$OUT"
(cd "$STAGE" && python3 -m zipfile -c "$OLDPWD/$OUT" ./* >/dev/null)
echo "built $OUT ($(du -h "$OUT" | cut -f1))"
