#!/usr/bin/env bash
# Runs inside the composite action (action.yml). Inputs arrive as INPUT_* variables.
set -euo pipefail

out_dir="$RUNNER_TEMP/upgrade-impact"
mkdir -p "$out_dir"
report="$out_dir/report.json"
rm -f "$report"

source="${INPUT_PR_URL:-${DEFAULT_PR_URL:-}}"
if [ -z "$source" ]; then
  echo "::error title=upgrade-impact::no pull request to check: run on a pull_request event or set the pr-url input"
  echo "verdict=error" >> "$GITHUB_OUTPUT"
  exit 2
fi

args=(check "$source" --format text --json-out "$report" --fail-on "${INPUT_FAIL_ON:-never}")
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  args+=(--summary-file "$GITHUB_STEP_SUMMARY")
fi
if [ "${INPUT_ANNOTATE:-true}" = "true" ]; then
  args+=(--annotate)
fi
if [ -n "${INPUT_REF:-}" ]; then
  args+=(--ref "$INPUT_REF")
fi
only="${INPUT_ONLY:-}"
for package in ${only//,/ }; do
  args+=(--only "$package")
done
[ -n "${INPUT_MODEL:-}" ] && args+=(--model "$INPUT_MODEL")
[ -n "${INPUT_BASE_URL:-}" ] && args+=(--base-url "$INPUT_BASE_URL")

# A model is used only when one is configured: an explicit provider, a base URL,
# or a provider's credentials in the step's environment.
has_model=""
for var in INPUT_PROVIDER INPUT_BASE_URL UPGRADE_IMPACT_PROVIDER UPGRADE_IMPACT_BASE_URL \
  ANTHROPIC_API_KEY AZURE_OPENAI_ENDPOINT OPENAI_API_KEY OLLAMA_HOST; do
  if [ -n "${!var:-}" ]; then has_model=1; fi
done
if [ "${INPUT_NO_LLM:-false}" = "true" ]; then
  args+=(--no-llm)
elif [ -z "$has_model" ]; then
  echo "::notice title=upgrade-impact::no model configured (set an API key in the step's env, or the provider/base-url inputs); listing breaking-looking note lines and import sites only"
  args+=(--no-llm)
elif [ -n "${INPUT_PROVIDER:-}" ]; then
  args+=(--provider "$INPUT_PROVIDER")
fi

set +e
"$UPGRADE_IMPACT_BIN" "${args[@]}"
code=$?
set -e

if [ -f "$report" ]; then
  "$UPGRADE_IMPACT_PY" - "$report" >> "$GITHUB_OUTPUT" <<'PY'
import json
import sys

from upgrade_impact.models import Report

report = Report.model_validate(json.load(open(sys.argv[1], encoding="utf-8")))
print(f"verdict={report.verdict}")
print(f"affected-lines={report.affected_lines()}")
print(f"packages={len(report.bumps)}")
PY
  echo "report-json=$report" >> "$GITHUB_OUTPUT"
else
  echo "verdict=error" >> "$GITHUB_OUTPUT"
fi
exit "$code"
