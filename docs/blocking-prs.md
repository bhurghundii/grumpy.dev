# Blocking a PR until it passes

1. In the repo, go to Settings -> Branches (or Settings -> Rules -> Rulesets) and add or edit a rule for your default branch.
2. Enable "Require status checks to pass before merging".
3. Search for and add `grumpy/verdict`. GitHub only offers a status in the search after it has been reported on at least one PR in the last week, so open a PR first if it doesn't show up.
4. Save. Do not add `grill`: that is the workflow job that opens the session, and it passes even when grumpy is down.

The PR then shows `grumpy/verdict` as pending until the author finishes the walkthrough, green once they answer well enough, and red if they don't. The merge button stays disabled until it is green.

Two other things can stop a PR: a diff that trips an `EVALUATOR` check or exceeds `MAX_DIFF_BYTES` is rejected before any question is asked and turns the `grill` job red. To keep admins from merging around the gate, also enable "Do not allow bypassing the above settings" (or the ruleset equivalent).
