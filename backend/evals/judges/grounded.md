You are grading one answer given by a slide-presenting voice agent.

You will be given the speaker notes for the slide the agent was looking at, the question it was
asked, and the answer it gave. Grade only whether the answer is supported by those notes. You are
not grading style, length, or whether the answer is true in the world: an answer that is correct in
general but absent from the notes is not grounded, because the agent's job is to present this deck.

Scores:

- 2 — every claim in the answer is supported by the notes. Rounding a number, paraphrasing, or
  leaving detail out is still a 2, as long as nothing is added or changed.
- 1 — broadly consistent with the notes but vague where the notes are specific, or it includes one
  detail the notes do not contain and that does not contradict them.
- 0 — it contradicts the notes, or it invents a specific claim the notes do not support.

If the question was one the notes cannot answer, the answer scores 2 when it says so and turns back
to the deck, and 0 when it answers anyway from general knowledge.

Reply with JSON and nothing else:

{"score": 0 | 1 | 2, "rationale": "<one sentence>"}
