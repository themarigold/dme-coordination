# DME back-end coordination: write-up

Eleanor Martinez is 72, on Original Medicare Part B with no Medigap, and needs a standard manual wheelchair (K0001). My prototype takes her case from the end of intake to a confirmed delivery. In the recorded run it got there in 9 simulated business days, with 30 Claude calls, $1.02 of model cost and 2 human touches, both of them approvals.

## Sequencing

I built the whole loop end to end, because the hard part of this job is the handoffs between Eleanor, her doctor's office and the supplier. Before writing code, I decided that rules would own every decision and Claude would only read, draft and suggest, and that set the build order. I built the deterministic core first (event loop, business-day clock, mock world, rules, approval inbox) and wrote the first eval items alongside it, before any prompt existed. Then I added the Claude layer, ran the case live end to end, and tuned the prompts against the eval. The rest of the time went into review by AI reviewers from different vendors, and at the end I stopped fixing and listed what was left as known limits.

The main scenario hits every failure in the brief: a supplier out of stock, one that doesn't answer, a wrong number, an order request the office lost, an order with the wrong code (K0003), a visit note missing a criterion, a supplier that goes silent before handoff (it switches to the backup on its own), and Eleanor confused about her cost. Tests cover two more: a supplier that isn't taking new patients or doesn't deliver to her ZIP, and a patient who can't be reached, where the system tries three calls on different days, her helper, a letter and her doctor's office before handing off to a person.

I wait for the written order and documented coverage criteria before any handoff. Medicare doesn't require the written order before delivering a K0001, but it must exist before the claim, and waiting avoids a denied claim. Suppliers also can't phone a Medicare patient about an item without her written permission, so Eleanor signs one for the chosen supplier before it gets her order.

## Technology & architecture

It's a Python command-line tool. Each case is one event loop on a simulated business-day clock, logged step by step to JSONL. Claude (`claude-opus-5-5`) is always called with structured outputs. Five readers turn supplier calls, office replies, the order, the visit note and Eleanor's replies into typed fields, and a sixth checks every reply for safety signals: an opt-out, a hospital stay, hospice, a death, Medicare Advantage or Medicaid. Any of those puts part of the case on hold until a person looks. Every field carries an evidence quote, and code drops the field if the quote isn't in what that person said. The writer drafts Eleanor's messages with placeholders that code fills in, so no number or date she reads was typed by a model. The proposer suggests one action from the list the rules allow, and code checks it and every fact it cites before a person sees it.

I kept AI out of anything that decides: which supplier qualifies, whether the order packet can go, what she owes and when a person is needed are rules in `dme/rules/`, and a person approves each packet before it goes. Her cost is Medicare's monthly rental amount, the $283 Part B deductible, then 20%, which for a mid-December delivery comes to about $72 to $326 over the 13-month rental, depending on whether her deductible is met. That message is a fixed template, sent only after the supplier confirms it accepts Medicare's rate for all 13 months and she has said she isn't on Medicaid. A reply that looks like a prompt injection goes to a person, and nothing in it is acted on except signals that make the case more careful.

The readers are graded against hand-labelled items. On 18 held-out items written by a separate author after tuning, they got 466 of 468 fields right and 17 of 18 items right on all three runs, and caught every safety signal. The set is small, so those numbers have wide error bars.

## The cut list

- **Real voice, phone and fax, eligibility checks and claim submission.** The mock world stands in for calls, and the rest is the supplier's job or outside what a coordinator does.
- **Full LLM planning.** Claude suggests a next step only when a case is stuck, and a person approves it. I'd rather earn more autonomy as the approval record grows than start there.
- **Regular updates to Eleanor.** She gets six messages over the case, and the supplier, not us, tells her the delivery date.
- **OCR, accessories, payment plans, risk levels per action, and an authorized-representative workflow.** Each is real work, but none changes how the loop runs.

## Known limits

These are choices I made, or edge cases I left open. None of them affects the recorded run.

- While a person has the handoff, "call this supplier again" moves it on if the supplier has since accepted, even when the person meant to ask about an open deposit. If the person has no open item, a supplier's reply is only logged, and a later acceptance clears an earlier rejection reason, such as a deposit. This is the one I'd fix first.
- The check that stops Claude writing about money is a word list, so a phrase like "on the house" gets through.
- A delivery closes once the supplier and Eleanor both confirm it. Unanswered fit, how-to-use or repair-number questions aren't chased.
- A request to pause counts as an unclear opt-out and goes to a person. A hedged "no" on Medicaid stays a "no", but a doubtful safety signal still escalates.
- A person can clear a false death report. Replies from the freeze aren't acted on and she's asked again, but that message can go without a follow-up, and a question she asked during the freeze goes unanswered.
- A visit note that names no patient is accepted, and the name check would treat "R. Martinez" or "Mr. Martinez" as Eleanor. Accepting the criteria is still a person's approval.
- Smaller gaps: a past date can pass as a delivery date, a birth date with no year can pick up a stray number as the year, "kilos" is read as pounds, an office saying it already sent the order to another supplier isn't handled, a failing approval suggestion keeps asking Claude until the 200-call cap stops the case, and the eval grader is stricter than production on hedged dates.

## What's next

With one more day: fix the known limits, starting with "call again". Run each reply's two reads at the same time, which would save about 52 of the 178 seconds spent waiting on Claude. Record the other two scenarios live, and send Eleanor a short update at each milestone, including her delivery date.

With two weeks, in this order: a real voice adapter for supplier calls; durable storage and timers, so cases survive restarts and many run at once; a bigger eval from real, consented call transcripts; and letting the approval switches loosen as the approval record grows. That order starts with the two things production can't run without and ends with the measurement that decides when a care advocate can step back.

## Constraints

- **Time.** I spent 4 hours, built the whole loop at prototype depth, and listed what I cut.
- **No auth, database or UI.** Cases live in memory with a JSONL log. Calls, faxes, e-signatures and every reply are mocked in `scenarios/*.yaml`, and the supplier directory is your 12 rows with nothing added. The Claude calls are real, and the README lists what's mocked.
- **Stack.** Python 3.12, Pydantic and the Anthropic SDK, which I'd also use in production, adding a Postgres event log, Temporal for the timers, a voice agent and a model under a BAA for patient data.
- **Where AI sits.** Claude reads, drafts and suggests; rules decide; people approve.
