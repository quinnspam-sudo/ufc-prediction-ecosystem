#!/usr/bin/env bash
# Called by workflow steps guarded with `if: failure()`.
#
# 1. Opens a GitHub issue labelled `ci-failure` (or comments on the one already
#    open), so GitHub emails the repo owner — works with no setup.
# 2. If the DISCORD_WEBHOOK secret is set, also posts to that Discord channel.
#
# Never fails the job itself: alerting must not mask the original failure.
set -u
RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
MSG="${GITHUB_WORKFLOW} failed on ${GITHUB_REF_NAME} ($(date -u +%Y-%m-%dT%H:%MZ)): ${RUN_URL}"

gh label create ci-failure --color B60205 \
  --description "Automated: a scheduled workflow failed" >/dev/null 2>&1 || true
OPEN=$(gh issue list --label ci-failure --state open --json number \
  --jq '.[0].number' 2>/dev/null || true)
if [ -n "${OPEN}" ]; then
  gh issue comment "${OPEN}" --body "${MSG}" >/dev/null 2>&1 || true
else
  gh issue create --label ci-failure --title "CI failure: ${GITHUB_WORKFLOW}" \
    --body "${MSG}

Close this issue once fixed; the next failure opens a new one." >/dev/null 2>&1 || true
fi

if [ -n "${DISCORD_WEBHOOK:-}" ]; then
  curl -s -m 15 -H "Content-Type: application/json" \
    -d "{\"content\": \"${MSG}\"}" "${DISCORD_WEBHOOK}" >/dev/null || true
fi
exit 0
