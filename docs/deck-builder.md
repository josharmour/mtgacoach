# Build a stronger deck

After a completed ranked Constructed game, Coach can offer: “Your recent
win/loss record suggests trying a new deck. Would you like help building one?”
The desktop popup offers **Help me build a deck** and **Not now**, and explains
the permanent entry point: **Tools → Decks → Build a stronger deck**.

The invitation uses wins / (wins + losses), requiring at least 20 results from
the most recent 30 in the same queue within 30 days. Draws and unknown results
do not count. This is player history across decks, not a measured result for a
particular deck. A persisted 24-hour cooldown prevents repeated invitations.

The expected rank-progress baseline is 33⅓% below Platinum and 50% at
Platinum/Diamond, following the [Wizards ranking overview](https://magic.wizards.com/en/mtgarena/getting-started).
The invitation triggers strictly below that baseline. Rank protection means
the baseline is not a hard minimum for eventual promotion. Mythic has no fixed
threshold, so it does not trigger this invitation. Limited and Traditional/Bo3
queues are excluded: recorded games cannot substitute for Bo3 match results.
Missing rank information suppresses the invitation.

Entering the builder turns off auto-queue. The player can re-enable it when
ready to play. At Arena's home screen, the builder reads collection and wildcard
balances, then offers **Owned cards only**, **Within my wildcard balance**, and
**All candidates** filters. Copies from different printings count together;
main deck and sideboard requirements add together. Unresolved cards are not
treated as affordable. Source lookups and collection comparisons run off the
desktop UI thread.

Automatic candidates currently use the top six Standard archetypes from
MTGGoldfish and their representative decklists. Source order and metagame share
are **popularity**, not Arena win rate or a promise of improved results. Each
candidate links to its source. Data may use the existing one-hour source cache.
Untapped is linked for researching Arena format/rank-specific performance;
users can paste exported lists to compare collection costs. There is no direct
Untapped statistics integration or fabricated performance score. 17Lands remains
the source for the existing Limited analysis; its card statistics are not used
to score Constructed decks.

**Copy deck for Arena import** copies a validated main-deck/sideboard export.
The player uses Arena's **Decks → Import**, reviews legality and missing cards,
and confirms any wildcard spending in Arena. Coach does not automatically craft
cards. Commander/companion exports are not supported by this checker yet.

Focused regressions: `tests/test_deck_recommendation.py`,
`tests/test_deck_builder.py`, and the existing performance/decks/editor tests.
