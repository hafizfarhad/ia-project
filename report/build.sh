#!/bin/bash
# Build the report and proposal PDFs.
#   ./report/build.sh            # uses the numbers already in report/generated/
#   ./report/build.sh --refresh  # first re-export results from the running system
set -euo pipefail
cd "$(dirname "$0")"
if [ "${1:-}" = "--refresh" ]; then ../.venv/bin/python ../scripts/export_results.py >/dev/null; fi
for doc in main proposal; do
  pdflatex -interaction=nonstopmode -halt-on-error "$doc" >/dev/null
  bibtex "$doc" >/dev/null
  pdflatex -interaction=nonstopmode -halt-on-error "$doc" >/dev/null
  pdflatex -interaction=nonstopmode -halt-on-error "$doc" | grep -E "Output written" || true
done
cp main.pdf WalletWatch-Report.pdf
cp proposal.pdf WalletWatch-Proposal.pdf
echo "built report/WalletWatch-Report.pdf and report/WalletWatch-Proposal.pdf"
