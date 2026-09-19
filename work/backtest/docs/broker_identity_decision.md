# Broker identity history decision

Status: `HISTORY_RETAINED_KNOWN_INACTIVE_IDENTIFIERS`

The broker account identifiers found in reachable Git history are retained as
historical findings. The account credentials are inactive according to the
task instruction, so this change applies a forward fix only. It does not
rewrite history, force-push, or delete branches or tags.

This status is an engineering decision, not user approval and not a claim that
history is clean. The scanner continues to publish raw historical findings;
the immutable baseline records only Git object identity, object type, opaque
denylist rule ID, and occurrence count.

Starting `origin/main` SHA: `023e39b288863ac93df9e7cfa1b7f218029f6458`.

The current-tree scan remains independent from historical baseline acceptance:
known historical objects do not exempt a new current-tree finding.
