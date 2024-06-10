LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || git rev-list --max-parents=0 HEAD)
echo "Last release tag or initial commit: $LAST_TAG"
MESSAGES=$(git log --no-merges --pretty=format:"- [%h](https://github.com/${{ github.repository }}/commit/%H) %s" "$LAST_TAG"..HEAD | awk '!seen[$0]++')
MESSAGES="${MESSAGES//$'\n'/'%0A'}"
echo "MESSAGES=$MESSAGES" >> "$GITHUB_OUTPUT"
echo "Commit messages since last release or initial commit:"
echo "$MESSAGES"