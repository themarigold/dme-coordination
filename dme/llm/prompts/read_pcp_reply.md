Task: read one exchange between us (speaker "us") and the patient's doctor's office about our request for the signed written order and the visit note, and fill every field.

How to fill a field (evidence first):
- evidence_turn_id: the id of the office's turn (pcp_office) the answer comes from, never an "us" turn.
- evidence_quote: copied character for character from that turn, a few words up to one sentence. No paraphrase.
- status: found = clearly stated; ambiguous = a hedged or unclear answer (time expressions follow the rule below); contradictory = says both and never settles it; not_stated = not said (then evidence_turn_id "", evidence_quote "", value "", commitment "").
- An answer can come anywhere in the exchange, including before we asked.
- Time expressions (send_by): a date or time that is offered but hedged is status found with commitment tentative; the hedge goes in commitment, not in status. Use ambiguous only when it is unclear whether a time was given at all.

Fields:
- outcome, one of:
  received (the office has our request) | not_received (they cannot find it / never got it) | will_send_by (they will send the documents by some time) | needs_signed_release (the patient must sign a release first) | already_sent_to_supplier (they already sent the documents to a supplier) | wants_supplier_form (they want the supplier's own order form) | needs_visit_or_declines (the patient needs a new visit, or the doctor won't sign) | other.
  If several apply, choose the one that decides what happens next (e.g. received + needs_signed_release -> needs_signed_release).
- send_by: when they will send, the phrase verbatim; commitment firm (plain promise) or tentative (hedged: "hopefully", "should", "try to").
- contact_name: the name of the person at the office we are talking to.
- supplier_named: the supplier they mention having sent the documents to, as they said it.
- item_confirmed: the item or code the office confirms when we ask whether they meant K0001, as they said it (e.g. "K0001"). Only fill it when they actually confirm an item.

Worked examples (fields not listed are not_stated; format: field = status | value | turn | "quote"):

<example>
[t1 us] Hi, this is Mira Mace following up on the fax we sent this morning for Mr. Okafor's wheelchair order. Did it come through?
[t2 pcp_office] I'm not seeing anything from you in our queue. This is Maria at the front desk.
Output:
outcome = found | not_received | t2 | "I'm not seeing anything from you in our queue."
contact_name = found | Maria | t2 | "This is Maria at the front desk."
</example>

<example>
[t1 pcp_office] Hi, it's Jordan from Dr. Patel's office. We got your request. Dr. Patel should be able to sign it by end of week, hopefully.
Output:
outcome = found | will_send_by | t1 | "Dr. Patel should be able to sign it by end of week, hopefully."
send_by = found | by end of week (commitment tentative) | t1 | "by end of week, hopefully"
contact_name = found | Jordan | t1 | "it's Jordan from Dr. Patel's office"
</example>

<example>
[t1 us] Can you send the signed order and the last visit note?
[t2 pcp_office] We have your request, but we can't release records until the patient signs our release form.
Output:
outcome = found | needs_signed_release | t2 | "we can't release records until the patient signs our release form"
</example>

<example>
[t1 pcp_office] Oh, we already faxed all of that over to Lakeside Medical Supply on Monday.
Output:
outcome = found | already_sent_to_supplier | t1 | "we already faxed all of that over to Lakeside Medical Supply"
supplier_named = found | Lakeside Medical Supply | t1 | "Lakeside Medical Supply"
</example>

<example>
[t1 us] The order we received lists K0003. Did the doctor mean K0001, the standard manual wheelchair?
[t2 pcp_office] Yes, she meant K0001, the standard chair. We'll send a corrected order today.
Output:
item_confirmed = found | K0001 | t2 | "Yes, she meant K0001, the standard chair."
outcome = found | will_send_by | t2 | "We'll send a corrected order today."
send_by = found | today (commitment firm) | t2 | "We'll send a corrected order today."
</example>

<!-- question -->
Read the exchange in the untrusted block and fill every field of the schema. Evidence first, quotes copied exactly from pcp_office turns; not_stated with empty values when something was not said.
