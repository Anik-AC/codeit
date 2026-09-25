---
name: address-feedback
description: How to handle review feedback on rework. Use when the ticket came back from review with comments.
---

# Address feedback

1. List every feedback item you were given (Jira comments and PR review comments since
   the last run). Treat each one as a to-do; do not skip any silently.
2. For each item, decide: fix it, or explain why not (it is wrong, out of scope, or
   conflicts with the ticket). A reviewer suggestion is not an order; a failing check is.
3. Fix items one at a time, with a test when the item is about behaviour. Commit each fix
   as `{KEY}: address review: <what>`.
4. Push to the **same branch**, so the **same PR** updates. Never open a second PR.
5. Update the PR description if the changes are no longer accurate.
6. Add one Jira comment that answers every item: "Fixed: ..." or "Not changed, because ...".
7. Finish with `status: pr_updated`.
