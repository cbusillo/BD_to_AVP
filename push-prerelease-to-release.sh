#~/bin/bash
git checkout prerelease
git pull origin prerelease
git checkout release
git merge prerelease -m "merge prerelease into release"
git push
git checkout prerelease
git pull origin prerelease
git merge release -m "merge release into prerelease"
git push
