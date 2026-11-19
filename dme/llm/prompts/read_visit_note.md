Task: read one clinical visit note sent by the doctor's office and decide, for each Medicare coverage criterion A to G for a manual wheelchair (their wording is in the rules below), whether the note documents it, and read the patient's weight. The whole note is one turn with turn id "doc".

How to fill a criterion (evidence first):
- documented: the note states facts that meet the criterion -> status found, value documented, evidence_turn_id "doc", evidence_quote copied character for character from the note (the sentence or phrase that meets it).
- contradictory: the note states something that goes against the criterion (e.g. it says a walker is enough) -> status contradictory, value contradictory, with the quote.
- unclear: the note touches the criterion but too vaguely to tell -> status ambiguous, value missing, with the quote.
- not addressed at all -> status not_stated, value missing, evidence_turn_id "", evidence_quote "".
Judge only what is written. Do not infer a criterion from the diagnosis alone, and do not treat a negated statement ("no caregiver at home") as meeting it.

Patient: beneficiary_name is the patient's name as the note gives it, with the quote (e.g. "Eleanor Martinez" from "Patient: Eleanor Martinez, 72F"); not stated -> not_stated, "".

Weight:
- weight_lb: the number in pounds as written, digits only (e.g. "158" from "Wt: 158 lbs"), with the quote. If the weight is given only in another unit, status ambiguous and value as written (e.g. "74 kg"). Not stated -> not_stated, "".

Worked examples (fields not listed are not_stated with value missing / ""; format: field = status | value | turn | "quote"):

<example>
[doc pcp_office] Visit 10/02/2026. 79F with severe knee OA. Unable to get to the bathroom or dress without stopping to rest; has fallen twice in the hallway. Uses a rolling walker but still cannot walk more than ten feet. Wt 171 lb. Home is single level with wide doorways. Will use a manual wheelchair daily at home and is eager to. Good upper body strength, able to self-propel.
Output:
criterion_a = found | documented | doc | "Unable to get to the bathroom or dress without stopping to rest"
criterion_b = found | documented | doc | "Uses a rolling walker but still cannot walk more than ten feet."
criterion_c = found | documented | doc | "Home is single level with wide doorways."
criterion_d = found | documented | doc | "Will use a manual wheelchair daily at home"
criterion_e = found | documented | doc | "is eager to"
criterion_f = found | documented | doc | "Good upper body strength, able to self-propel."
weight_lb = found | 171 | doc | "Wt 171 lb."
</example>

<example>
[doc pcp_office] Follow-up 10/09/2026. Patient reports trouble bathing due to hip pain. Ambulates household distances safely with a cane. Lives alone, no caregiver available. Discussed wheelchair; patient not sure she would use it.
Output:
criterion_a = found | documented | doc | "Patient reports trouble bathing due to hip pain."
criterion_b = contradictory | contradictory | doc | "Ambulates household distances safely with a cane."
criterion_e = ambiguous | missing | doc | "patient not sure she would use it"
criterion_g = contradictory | contradictory | doc | "no caregiver available"
weight_lb = not_stated | "" (the note gives no weight)
</example>

<!-- question -->
Read the visit note in the untrusted block and fill every field of the schema: each criterion A to G and the weight. Evidence turn "doc", quotes copied exactly; not_stated with value missing for a criterion the note does not address.
