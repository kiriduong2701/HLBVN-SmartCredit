"""Pin the policy content hash to its version (server/policy/policy.lock.json).

Usage: python3 scripts/policy_lock.py
Refuses to re-pin an already-locked version to a different hash: changing any threshold requires a policy_version bump
(audit section 4: "a changed threshold without a version bump is not detectable"). The test suite fails while the lock
and the policy disagree.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from server.engine import policy_integrity as PI  # noqa: E402

POLICY = os.path.join(ROOT, "server", "policy", "demo_policy.json")
LOCK = os.path.join(ROOT, "server", "policy", "policy.lock.json")


def main() -> int:
    policy = json.load(open(POLICY))
    errors = PI.validate(policy)
    if errors:
        print("policy invalid:", errors)
        return 1
    h, v = PI.policy_hash(policy), policy["policy_version"]
    lock = json.load(open(LOCK)) if os.path.exists(LOCK) else {"history": []}
    prior = {x["policy_version"]: x["sha256"] for x in lock.get("history", [])}
    if v in prior and prior[v] != h:
        print("REFUSED: policy %s is already locked to %s; bump policy_version for changed content." % (v, prior[v][:12]))
        return 2
    if v not in prior:
        lock.setdefault("history", []).append({"policy_version": v, "sha256": h})
    lock.update(policy_version=v, sha256=h)
    json.dump(lock, open(LOCK, "w"), indent=2)
    print("locked", v, h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
