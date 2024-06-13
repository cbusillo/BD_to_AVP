#!/bin/bash

if [ "$1" == "r" ]; then
  SOURCE_BRANCH="prerelease"
  DESTINATION_BRANCH="release"
elif [ "$1" == "p" ]; then
  SOURCE_BRANCH="master"
  DESTINATION_BRANCH="prerelease"
else
  echo "Please provide the destination branch name as an argument."
  exit 1
fi

git checkout "$SOURCE_BRANCH"
git pull origin "$SOURCE_BRANCH"
git checkout "$DESTINATION_BRANCH"
git merge "$SOURCE_BRANCH" -m "merge $SOURCE_BRANCH into $DESTINATION_BRANCH"
git push
git checkout "$SOURCE_BRANCH"
git pull origin "$SOURCE_BRANCH"
git merge "$DESTINATION_BRANCH" -m "merge $DESTINATION_BRANCH into $SOURCE_BRANCH"
