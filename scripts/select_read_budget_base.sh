#!/bin/sh
# Print the commit the read-budget gate compares against.
#
# push: the first parent of HEAD. A push to main is already main's tip, so the
# merge base with origin/main is HEAD itself and would compare the code with itself.
# any other event: the merge base of HEAD with origin/main. A pull request checkout
# is the merge ref, so that base is main's tip.
set -eu
event=${1:-}
if [ "$event" = "push" ]; then
  git rev-parse HEAD^1
else
  git merge-base HEAD origin/main
fi
