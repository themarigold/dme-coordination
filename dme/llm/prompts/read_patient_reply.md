Task: read one exchange between us (speaker "us") and the patient (speaker "patient") or someone helping her (speaker "helper"), and fill every field. Our turns show which question is being answered; they are never evidence.

How to fill a field (evidence first):
- evidence_turn_id: the id of the patient's or helper's turn the answer comes from.
- evidence_quote: copied character for character from that turn, a few words up to one sentence. No paraphrase.
- status: found = clearly stated; ambiguous = a hedged or unclear answer ("I think so", "maybe"; time expressions follow the rule below); contradictory = says both and never settles it; not_stated = not said (then evidence_turn_id "", evidence_quote "", value "", commitment "").
- A "no" is an answer ("never had one" -> found, no). An answer can come before we asked it.
- Time expressions (availability): a date or time that is offered but hedged is status found with commitment tentative; the hedge goes in commitment, not in status. Use ambiguous only when it is unclear whether a time was given at all.
- A helper speaking about the patient counts; a helper's own name goes in helper_name.

Fields:
- intents: one entry per thing she does in the reply, each with its own evidence: confirm (says yes to our question) | decline (says no to what we asked) | gives_fact | cost_question (asks what it will cost) | other_question (any other question) | opt_out (wants no more contact: a clear request to stop is status found; a request to pause, hold off or wait is status ambiguous, with the quote, so a person checks it) | wants_person (asks for a real person). Empty list if none.
- identity_name, identity_dob: her name and date of birth as she says them.
- medicare_id: her Medicare number as said.  medicare_id_ok_to_share: yes/no to sharing it with the supplier.
- address, zip: as said.  phone_source: where she says we got her number / that the number is hers.
- in_home_use: yes if she will use the wheelchair in her home.
- home_layout: her description of the home (stairs, doorways, rooms), as said.
- prior_wheelchair_5y: had a wheelchair in the last five years.
- medicaid_or_msp: yes | no | not_sure — whether she gets help paying Medicare costs from Medicaid or a Medicare Savings Program ("I don't know" -> not_sure, status found).
- preferred_supplier: a supplier she prefers, as named.
- helper_name, helper_permission: someone who helps her, and whether we may talk to them.
- consent_proceed, consent_share_with_suppliers, consent_calls_texts: her yes/no to each of those questions.
- supplier_call_permission: her yes/no to the named supplier calling her.
- availability: when she is available for delivery, phrase verbatim; commitment firm or tentative.
- delivery_received, fit_ok, shown_how_to_use, has_repair_number: after delivery, her yes/no to each.

Worked examples (fields not listed are not_stated; format: field = status | value | turn | "quote"):

<example>
[t1 us] To make sure I'm speaking with the right person, could you tell me your full name and date of birth?
[t2 patient] Gloria Reyes, March 3rd, 1951. And yes, go ahead with all of it, you can share my details with the supply companies.
Output:
identity_name = found | Gloria Reyes | t2 | "Gloria Reyes"
identity_dob = found | March 3rd, 1951 | t2 | "March 3rd, 1951"
consent_proceed = found | yes | t2 | "yes, go ahead with all of it"
consent_share_with_suppliers = found | yes | t2 | "you can share my details with the supply companies"
intents = [gives_fact | t2 | "Gloria Reyes, March 3rd, 1951", confirm | t2 | "yes, go ahead with all of it"]
</example>

<example>
[t1 us] Have you had a wheelchair in the last five years?
[t2 patient] No, never. My apartment has two steps at the front door and the hallway is narrow, if that matters.
Output:
prior_wheelchair_5y = found | no | t2 | "No, never."
home_layout = found | two steps at the front door and the hallway is narrow | t2 | "My apartment has two steps at the front door and the hallway is narrow"
intents = [gives_fact | t2 | "No, never."]
</example>

<example>
[t1 us] Do you get help paying your Medicare costs from Illinois Medicaid or a Medicare Savings Program?
[t2 helper] I'm her daughter, Ana. Honestly I'm not sure, she gets some letters from the state, I'd have to look. You can talk to me about all this, she said that's fine.
[t3 patient] Yes, talk to Ana. How much is this chair going to cost me?
Output:
medicaid_or_msp = found | not_sure | t2 | "Honestly I'm not sure, she gets some letters from the state"
helper_name = found | Ana | t2 | "I'm her daughter, Ana."
helper_permission = found | yes | t3 | "Yes, talk to Ana."
intents = [gives_fact | t2 | "Honestly I'm not sure", confirm | t3 | "Yes, talk to Ana.", cost_question | t3 | "How much is this chair going to cost me?"]
</example>

<example>
[t1 us] When would be a good time for the supplier to deliver?
[t2 patient] Probably weekday mornings, I think. Actually, can I just talk to a real person about this?
Output:
availability = found | weekday mornings (commitment tentative) | t2 | "Probably weekday mornings, I think."
intents = [gives_fact | t2 | "Probably weekday mornings, I think.", wants_person | t2 | "can I just talk to a real person about this?"]
</example>

<!-- question -->
Read the exchange in the untrusted block and fill every field of the schema, including the intents list. Evidence first, quotes copied exactly from patient or helper turns; not_stated with empty values when something was not said.
