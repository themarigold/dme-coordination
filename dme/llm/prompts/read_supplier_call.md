Task: read one phone call between us (speaker "us") and a medical equipment supplier, and fill every field.

How to fill a field (evidence first):
- evidence_turn_id: the id of the turn the answer comes from. It must be a turn spoken by the supplier side (supplier_rep, or unknown for whoever picked up), never an "us" turn. Our turns only tell you which question is being answered.
- evidence_quote: copied character for character from that turn, a few words up to one sentence that contains the answer. Do not paraphrase, fix typos, or stitch pieces together.
- status: found = clearly stated; ambiguous = a hedged or unclear answer ("I think so", "I'd have to check"; time expressions follow the rule below); contradictory = the supplier says both and never settles it; not_stated = not said.
- value: the answer (lower-case for yes/no and fixed values). For ambiguous or contradictory, your best reading, or "".
- not_stated means evidence_turn_id "", evidence_quote "", value "" (and commitment "").
- Time expressions (earliest_delivery, call_back_after): a date or time that is offered but hedged is status found with commitment tentative; the hedge goes in commitment, not in status. Use ambiguous only when it is unclear whether a time was given at all.
- An answer can come anywhere, including before we asked. A "no" is an answer: "we're out of those" is k0001_in_stock = found, no.

Fields:
- call_outcome: answered (a person talked with us) | voicemail | no_answer | call_back_later (asked us to call back) | wrong_number (not this business, or disconnected) | closed (out of business / permanently closed).
- call_back_after: when to call back, the phrase verbatim (e.g. "after four"); commitment firm or tentative.
- accepting_new_medicare: taking new Medicare patients.
- bills_medicare_k0001: can bill Medicare for a K0001 standard manual wheelchair.
- k0001_in_stock: has a K0001 / standard manual wheelchair in stock.
- delivers_to_zip: delivers to the patient's ZIP code.
- accepts_assignment_all_months: accepts Medicare assignment for all of the rental months.
- abn_required: the patient would have to sign an ABN (Advance Beneficiary Notice).
- earliest_delivery: the earliest delivery the supplier gives, phrase verbatim; commitment firm (a plain statement) or tentative (hedged: "probably", "maybe", "should be", "if").
- rep_name: the supplier person's own name as they give it (not someone they refer us to).
- readback_confirmed: yes when the supplier confirms our read-back of the answers, in their turn right after our read-back; no if they correct it.
- needs_info: yes when the supplier needs something from us before going ahead; needs_info_item: medicare_id | demographics | order_copy | other.
- collects_coinsurance_at_delivery: yes when the supplier says it will bill or collect the patient's 20% / deductible at or after delivery.
- full_price_or_no_assignment: yes when the supplier wants full price up front, or will not bill Medicare on assignment.
- other_condition: any other condition the supplier attaches, as they said it; "" if none.

Worked examples (fields not listed are not_stated; format: field = status | value | turn | "quote"):

<example>
[t1 us] Hi, this is Mira Mace calling about a standard manual wheelchair, K0001, for a Medicare patient in 60601. Are you taking new Medicare patients?
[t2 supplier_rep] We are, yes. This is Dana.
[t3 us] Can you bill Medicare for a K0001, and is one in stock?
[t4 supplier_rep] We bill Medicare for those all the time, and we have a few on the shelf.
[t5 us] Do you deliver to 60601, and will you take assignment for every rental month?
[t6 supplier_rep] That ZIP is in our area. Assignment the whole rental, yes.
[t7 us] Would she need to sign an ABN? And what's the earliest delivery?
[t8 supplier_rep] No ABN for a standard chair. We can deliver Tuesday. She pays her 20 percent once it's delivered.
[t9 us] Let me read that back: new Medicare patients yes, K0001 billing yes, in stock yes, delivers to 60601 yes, assignment all months yes, no ABN, delivery Tuesday. Is that right?
[t10 supplier_rep] Yep, that's all right.
Output:
call_outcome = found | answered | t2 | "We are, yes."
accepting_new_medicare = found | yes | t2 | "We are, yes."
rep_name = found | Dana | t2 | "This is Dana"
bills_medicare_k0001 = found | yes | t4 | "We bill Medicare for those all the time"
k0001_in_stock = found | yes | t4 | "we have a few on the shelf"
delivers_to_zip = found | yes | t6 | "That ZIP is in our area."
accepts_assignment_all_months = found | yes | t6 | "Assignment the whole rental, yes."
abn_required = found | no | t8 | "No ABN for a standard chair."
earliest_delivery = found | Tuesday (commitment firm) | t8 | "We can deliver Tuesday."
collects_coinsurance_at_delivery = found | yes | t8 | "She pays her 20 percent once it's delivered."
readback_confirmed = found | yes | t10 | "Yep, that's all right."
</example>

<example>
[t1 us] Hi, calling about a K0001 standard wheelchair for a Medicare patient. Do you have one in stock?
[t2 supplier_rep] Not right now, no, we're out of standard chairs until the next truck. I can't answer the billing stuff anyway, call back after four and ask for Luis.
Output:
call_outcome = found | call_back_later | t2 | "call back after four"
call_back_after = found | after four (commitment firm) | t2 | "call back after four"
k0001_in_stock = found | no | t2 | "Not right now, no, we're out of standard chairs"
(rep_name stays not_stated: Luis is someone else, not the person speaking.)
</example>

<example>
[t1 supplier_rep] Northside Medical, Pat speaking. If this is Medicare, heads up, we don't do assignment. Patients pay us in full and Medicare reimburses them.
[t2 us] Understood. Do you deliver to 60615?
[t3 supplier_rep] I think so? Our driver goes down there sometimes, I'd have to check.
Output:
call_outcome = found | answered | t1 | "Pat speaking"
rep_name = found | Pat | t1 | "Pat speaking"
accepts_assignment_all_months = found | no | t1 | "we don't do assignment"
full_price_or_no_assignment = found | yes | t1 | "Patients pay us in full and Medicare reimburses them."
delivers_to_zip = ambiguous | yes | t3 | "I think so? Our driver goes down there sometimes, I'd have to check."
</example>

<example>
[t1 us] Do you have a K0001 for a Medicare patient, and can you bill Medicare for it?
[t2 supplier_rep] We do and we can. Before I hold one I'll need her Medicare number though.
Output:
call_outcome = found | answered | t2 | "We do and we can."
k0001_in_stock = found | yes | t2 | "We do and we can."
bills_medicare_k0001 = found | yes | t2 | "We do and we can."
needs_info = found | yes | t2 | "I'll need her Medicare number"
needs_info_item = found | medicare_id | t2 | "I'll need her Medicare number"
</example>

<example>
[t1 us] Hello, is this Westgate Home Medical?
[t2 unknown] No, you've got the wrong number, this is a house.
Output:
call_outcome = found | wrong_number | t2 | "you've got the wrong number"
</example>

<!-- question -->
Read the call in the untrusted block and fill every field of the schema. Evidence first, quotes copied exactly from supplier-side turns; not_stated with empty values when something was not said.
