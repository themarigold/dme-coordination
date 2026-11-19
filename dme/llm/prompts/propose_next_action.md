Task: a case has stopped and needs a decision (an inbox item). Propose exactly one next action from the allowed list, for a human to approve. You do not carry it out; code checks it.

Rules:
- action: the name of one allowed action (the part before any parenthesis). Never an action that is not in the list.
- Parameters: fill only the ones the action needs; set every other parameter to "" (or [] for param_items).
  - param_party: "pcp" | "patient" | "supplier:<id>" (call_again).
  - param_supplier: "supplier:<id>" (send_packet_to, release_supplier).
  - param_items: fact names to request from the doctor's office (request_from_pcp), e.g. ["criterion_b", "weight_lb"].
  - param_question: a key from the fixed patient question list (ask_patient).
  - param_time_phrase: when to wait until, verbatim (wait_until).
  - param_reason: the escalation reason code (escalate_to_human).
  If an allowed-list entry already shows a parameter value (e.g. "release_supplier(supplier:4)"), use that value.
- On an approval item (reason code approval_required; the allowed list offers proceed_with_pending / hold_pending), the goal names the one gated action waiting for approval. proceed_with_pending approves ONLY that gated action, nothing else. What each gated action relies on:
  - Accepting the visit-note criteria (sends nothing): criteria A, B, D and E documented, F or G documented, and a weight of 250 lb or less. Criterion C may be missing (the supplier's home assessment covers it later).
  - Sending the order packet to a supplier (a supplier handoff): that supplier's status is primary; the patient's written permission for that supplier to call her (supplier.<id>.call_permission = yes); her consent to share with suppliers (patient.consent_share_with_suppliers = yes); the order elements complete (order.elements_complete = yes); the criteria accepted (note.criteria_accepted = yes); and no open hold (the "holds:" line says none).
  - Any other gated action: what the goal says proceeding does.
  - Choose proceed_with_pending when everything on the gated action's list is shown. Choose hold_pending when anything on that list is missing, empty or wrong, say which in why, and cite the facts you used. Facts with no value are left out of the case summary, so a needed fact that is not listed counts as missing.
  - A hold_pending must cite the fact or facts it is waiting on, including a missing one: cite a missing fact with its fact id and an empty value (""). A hold that cites no facts is rejected.
  - Only issues outside the gated action's list are not reasons to hold. For example, when accepting the criteria, the order's validity or item code, consent, holds and supplier status are not reasons to hold: the rules check them separately before anything is sent.
- why: one sentence, plain words, naming the deciding facts.
- facts_used: the facts your choice relies on, each as fact_id and value copied exactly, character for character, from a "fact_id = value" line in the facts block or in the case summary. Never a changed value, and never a fact id that is not listed, except a missing fact a hold_pending waits on, cited with the value "". Always cite at least one fact; usually two to six.
- Prefer the action that moves the case forward safely with the fewest human touches; escalate_to_human only when no other allowed action fits.

Worked examples (the "fact_id = value" lines would come in the case summary or the facts block):

<example>
Reason code: approval_required. Allowed: proceed_with_pending, hold_pending.
Goal: Approval required: accept the visit-note criteria A-G for the packet (proceeding sends nothing; the order and packet are gated separately).
Facts: note.criterion_a = documented; note.criterion_b = documented; note.criterion_c = missing; note.criterion_d = documented; note.criterion_e = documented; note.criterion_f = missing; note.criterion_g = documented; note.weight_lb = 204; order.hcpcs_on_order = K0004; order.item_matches_k0001 = no.
Output: {"action": "proceed_with_pending", "param_party": "", "param_supplier": "", "param_items": [], "param_question": "", "param_time_phrase": "", "param_reason": "", "why": "The visit note documents criteria A, B, D, E and G and a weight of 204 lb; the order's item code is a separate check and does not affect accepting the criteria.", "facts_used": [{"fact_id": "note.criterion_a", "value": "documented"}, {"fact_id": "note.criterion_b", "value": "documented"}, {"fact_id": "note.criterion_d", "value": "documented"}, {"fact_id": "note.criterion_e", "value": "documented"}, {"fact_id": "note.criterion_g", "value": "documented"}, {"fact_id": "note.weight_lb", "value": "204"}]}
</example>

<example>
Reason code: approval_required. Allowed: proceed_with_pending, hold_pending.
Goal: Approval required: accept the visit-note criteria A-G for the packet (proceeding sends nothing; the order and packet are gated separately).
Facts: note.criterion_a = documented; note.criterion_b = contradictory; note.criterion_d = documented; note.criterion_e = documented; note.criterion_f = documented; note.weight_lb = 171.
Output: {"action": "hold_pending", "param_party": "", "param_supplier": "", "param_items": [], "param_question": "", "param_time_phrase": "", "param_reason": "", "why": "Criterion B is contradictory in the visit note, so the criteria cannot be accepted yet.", "facts_used": [{"fact_id": "note.criterion_b", "value": "contradictory"}]}
</example>

<example>
Reason code: approval_required. Allowed: proceed_with_pending, hold_pending.
Goal: Approval required: send the order packet to supplier 7 (Oakwood Medical Supply) (proceeding sends the order, the visit note and Ruth Okafor's delivery details to that supplier; nothing else).
Facts: holds: none; handoff.supplier = 7; supplier.7.status = primary; supplier.7.call_permission = yes; patient.consent_share_with_suppliers = yes; order.elements_complete = yes; note.criteria_accepted = yes.
Output: {"action": "proceed_with_pending", "param_party": "", "param_supplier": "", "param_items": [], "param_question": "", "param_time_phrase": "", "param_reason": "", "why": "Supplier 7 is the primary, Ruth gave written permission for it to call her and consented to sharing, the order is complete, the criteria are accepted and nothing is on hold.", "facts_used": [{"fact_id": "supplier.7.status", "value": "primary"}, {"fact_id": "supplier.7.call_permission", "value": "yes"}, {"fact_id": "patient.consent_share_with_suppliers", "value": "yes"}, {"fact_id": "order.elements_complete", "value": "yes"}, {"fact_id": "note.criteria_accepted", "value": "yes"}]}
</example>

<example>
Reason code: approval_required. Allowed: proceed_with_pending, hold_pending.
Goal: Approval required: send the order packet to supplier 3 (Maple Street Home Care) (proceeding sends the order, the visit note and Ruth Okafor's delivery details to that supplier; nothing else).
Facts: holds: none; handoff.supplier = 3; supplier.3.status = primary; patient.consent_share_with_suppliers = yes; order.elements_complete = yes; note.criteria_accepted = yes. (No supplier.3.call_permission line.)
Output: {"action": "hold_pending", "param_party": "", "param_supplier": "", "param_items": [], "param_question": "", "param_time_phrase": "", "param_reason": "", "why": "Ruth has not yet given written permission for supplier 3 to call her, so the packet cannot go to it yet.", "facts_used": [{"fact_id": "handoff.supplier", "value": "3"}, {"fact_id": "supplier.3.status", "value": "primary"}, {"fact_id": "supplier.3.call_permission", "value": ""}]}
</example>

<example>
Reason code: second_handoff. Allowed: release_supplier(supplier:2), send_packet_to(supplier:9), escalate_to_human.
Facts: supplier.2.packet_sent = yes; supplier.2.reject_reason = out_of_stock; supplier.9.status = backup.
Output: {"action": "send_packet_to", "param_party": "", "param_supplier": "supplier:9", "param_items": [], "param_question": "", "param_time_phrase": "", "param_reason": "", "why": "Supplier 2 rejected the packet as out of stock and supplier 9 is the qualified backup.", "facts_used": [{"fact_id": "supplier.2.reject_reason", "value": "out_of_stock"}, {"fact_id": "supplier.9.status", "value": "backup"}]}
</example>

<!-- question -->
Propose one next action from the allowed list as JSON matching the schema: action, its parameters (others empty), a one-sentence why, and facts_used copied exactly from the facts block or the case summary.
