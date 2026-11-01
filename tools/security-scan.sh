#!/bin/sh
# 0.67: the code and dependency checks an auditor will ask about. Run from the top folder of Dental OS.
#
#   1. Dental OS's own checks (no internet needed): the same as code_checks.txt in the evidence pack.
#   2. Bandit, a code scanner for Python, if installed:   pip install bandit
#   3. pip-audit, which looks up every installed package in the public list of known holes, if installed:   pip install pip-audit
#
# 2 and 3 need to reach PyPI and the vulnerability database, so run them on a computer with internet. Keep the output with the
# evidence pack. Exit code 1 if any check finds something.
set -u
status=0

echo "== Dental OS code checks"
python3 -c "from dental_os.services import seccheck; r = seccheck.report(); print(r); raise SystemExit(0 if 'Findings: 0' in r else 1)" || status=1

if command -v bandit >/dev/null 2>&1; then
  echo "== Bandit (medium and high only)"
  bandit -q -r dental_os -ll || status=1
else
  echo "== Bandit is not installed here. pip install bandit, then run this again."
fi

if command -v pip-audit >/dev/null 2>&1; then
  echo "== pip-audit"
  pip-audit -r requirements.txt || status=1
else
  echo "== pip-audit is not installed here. pip install pip-audit, then run this again."
fi

exit $status
