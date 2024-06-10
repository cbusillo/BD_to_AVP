#~/bin/bash
git checkout release
git merge master -m "merge master into release"
git push
git checkout master
git pull origin release
git merge release -m "merge release into master"
git push
