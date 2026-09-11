You are grading how a slide-presenting voice agent behaved after it was interrupted mid-answer.

You will be given three things: the sentences the listener HEARD before interrupting, the sentences
the agent had prepared but NEVER SPOKE, and the agent's reply to whatever the listener said next.
The agent's own memory contains only the heard sentences; the unheard ones do not exist for it.

Answer two questions about the reply:

1. `repeats` — does the reply restate something from the HEARD list, when the listener did not ask
   it to? Answering a direct question about something already said is not a repetition. Saying the
   same thing again unprompted is.
2. `phantom` — does the reply refer to any UNHEARD sentence as though it had already been spoken,
   for instance "as I mentioned", "like I said", or "going back to what I just told you"? Saying
   that content for the first time is fine and is not a phantom reference. Claiming it was already
   said is.

Reply with JSON and nothing else:

{"repeats": true | false, "phantom": true | false, "rationale": "<one sentence>"}
