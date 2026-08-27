# Reference repository review

Reviewed: https://github.com/rjm2007/aws_deployed_raush_pt at commit
`db012ba51c687192b02d2041a960619dd29fe673` on 2026-08-24.

## Adopted patterns

- Compatibility with Vapi's current and legacy tool-call envelopes.
- Vapi top-level static parameters make call-start lead and outreach-event IDs invisible to and
  non-overridable by the model; the API retains envelope compatibility as a second check.
- Patient-friendly 12-hour appointment times in voice and confirmation responses.
- Explicit startup/readiness checks for required configuration and dependencies.

## Deliberately not copied

- Tebra-specific, inbound-call, multi-location, reminder, cancellation, and rescheduling workflows are
  outside this milestone.
- Raw request bodies, names, phones, and appointment data must not be written to application logs.
- Fixed UTC offsets are not safe across daylight-saving transitions; this project uses IANA timezones.
- The reference notification check-then-send flow is replaced by a database-owned durable send state and
  ambiguity handling so an uncertain SMS is never retried automatically.
