# Token lifecycle - design trade-offs

## 1. Token storage: embedded on the user record vs separate collection
- Chosen: Embed the signed token plus an expiry stamp directly on the user document (confirmation_token / token_issued_at / token_expires_at / token_redeemed_at).
- Rejected: A standalone tokens collection with its own lifecycle.
- Consequences:
  - Resend rotates the old link: reissuing overwrites the embedded token, so links from older emails can no longer match any record.
  - Concurrent double clicks are idempotent while the redeemed residue (token_redeemed_at) is retained: the second click resolves to already_active instead of activating twice.
  - There are no orphan token rows to reap across collections; the worker sweep clearing the four fields is the only reaper.

## 2. Expiry comparison: UTC strict greater-than vs local / greater-than-or-equal
- Chosen: Compare now > token_expires_at using UTC epoch seconds, strict greater-than.
- Rejected: Local server time with >=, or the serializer's own clock.
- Consequences:
  - UTC avoids a deployed server drifting between timezones/DST versus the issued stamp.
  - At the exact boundary second (now == token_expires_at) the link is still valid; >= would expire a link the very instant it is issued when stamps coincide, while a naive/never-set local comparison can make a link live forever.

## 3. Confirmation failure: inline branch vs redirect to login
- Chosen: Default renders confirm.html inline with an explicit reason branch (invalid / expired / already_active / user_not_found); a redirect-to-login disposition is also implemented and is counted separately.
- Rejected: Always redirecting every failure to /login with one generic error.
- Consequences:
  - The two paths are distinguishable in the report: inline_failures vs redirect_failures counters.
  - The three failure classes keep their own counters, so 'link invalid', 'account already active' and 'user does not exist' are never merged into one opaque bucket.
