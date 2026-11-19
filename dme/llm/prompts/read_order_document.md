Task: read one written order (a Standard Written Order for durable medical equipment) sent by the doctor's office, and fill every field. The whole document is one turn with turn id "doc".

How to fill a field (evidence first):
- evidence_turn_id: "doc" for anything found in the document.
- evidence_quote: copied character for character from the document, a few words up to one line. No paraphrase, no fixing typos.
- status: found = clearly present; ambiguous = present but unclear or illegible; contradictory = the document says two different things and does not settle which one stands (e.g. a code changed without the practitioner's initials or signature and a date); not_stated = absent (then evidence_turn_id "", evidence_quote "", value "").

Fields (value = the text exactly as written, unless said otherwise):
- beneficiary_name, medicare_id: the patient's name and Medicare number (MBI) as written.
- item_description: the name of the item as written (e.g. "Standard manual wheelchair"), keeping every word that names the item type (e.g. "heavy duty", "lightweight"), but without the code and without option or accessory lists.
- hcpcs_code: the code exactly as written, even if it looks wrong (e.g. "KOOO1" with letter O stays "KOOO1"). If a code is crossed out and replaced:
  - the change carries the practitioner's initials or signature, and a date -> the replacement code, status found;
  - the change lacks the practitioner's initials or signature, or the date -> the page shows two codes and neither is settled: status contradictory, value the replacement code, quote the change.
  Either way, report the change in has_changes and changes_initialled_dated.
- quantity, order_date: as written.
- practitioner_name, practitioner_npi: the ordering practitioner's name and NPI as written.
- signature_present: yes if the practitioner's signature is there (a signature, "/s/ name", "electronically signed by ..."); no if the signature line is blank or says "signature on file pending" or similar.
- has_changes: yes if anything on the order was crossed out, overwritten, added by hand, or marked as corrected; no if the order is clean (quote the item line as evidence).
- changes_initialled_dated: yes only if every change carries the practitioner's initials or signature AND a date; no if any change lacks either; not_stated if there are no changes.

Worked examples (fields not listed are not_stated; format: field = status | value | turn | "quote"):

<example>
[doc pcp_office] STANDARD WRITTEN ORDER
  Patient: Robert Hale   MBI: 4XK2-TE5-MN81
  Item: Standard manual wheelchair (K0001)   Qty: 1
  Order date: 09/14/2026
  Ordering practitioner: Amy Lin, MD   NPI 1234567893
  Signature: /s/ Amy Lin, MD 09/14/2026
Output:
beneficiary_name = found | Robert Hale | doc | "Patient: Robert Hale"
medicare_id = found | 4XK2-TE5-MN81 | doc | "MBI: 4XK2-TE5-MN81"
item_description = found | Standard manual wheelchair | doc | "Item: Standard manual wheelchair (K0001)"
hcpcs_code = found | K0001 | doc | "Standard manual wheelchair (K0001)"
quantity = found | 1 | doc | "Qty: 1"
order_date = found | 09/14/2026 | doc | "Order date: 09/14/2026"
practitioner_name = found | Amy Lin, MD | doc | "Ordering practitioner: Amy Lin, MD"
practitioner_npi = found | 1234567893 | doc | "NPI 1234567893"
signature_present = found | yes | doc | "Signature: /s/ Amy Lin, MD 09/14/2026"
has_changes = found | no | doc | "Item: Standard manual wheelchair (K0001)"
</example>

<example>
[doc pcp_office] ORDER - DME
  Patient: Robert Hale
  Item: wheelchair, lightweight K0004 [crossed out] K0001 handwritten
  Quantity: 1   Date: 09/20/2026
  Physician: Amy Lin, MD
  Signature: ____________
Output:
hcpcs_code = contradictory | K0001 | doc | "K0004 [crossed out] K0001 handwritten"
item_description = ambiguous | wheelchair | doc | "Item: wheelchair, lightweight K0004 [crossed out] K0001 handwritten"
has_changes = found | yes | doc | "K0004 [crossed out] K0001 handwritten"
changes_initialled_dated = found | no | doc | "K0004 [crossed out] K0001 handwritten"
signature_present = found | no | doc | "Signature: ____________"
quantity = found | 1 | doc | "Quantity: 1"
order_date = found | 09/20/2026 | doc | "Date: 09/20/2026"
practitioner_name = found | Amy Lin, MD | doc | "Physician: Amy Lin, MD"
beneficiary_name = found | Robert Hale | doc | "Patient: Robert Hale"
</example>

<example>
[doc pcp_office] Corrected order. Item: standard wheelchair, code K0003 struck through, K0001 written above, initialled AL 9/22/26.
  Signed: Amy Lin MD (electronically signed 09/22/2026)
Output:
hcpcs_code = found | K0001 | doc | "K0001 written above"
has_changes = found | yes | doc | "code K0003 struck through, K0001 written above"
changes_initialled_dated = found | yes | doc | "initialled AL 9/22/26"
signature_present = found | yes | doc | "electronically signed 09/22/2026"
item_description = found | standard wheelchair | doc | "Item: standard wheelchair"
practitioner_name = found | Amy Lin MD | doc | "Signed: Amy Lin MD"
</example>

<!-- question -->
Read the order in the untrusted block and fill every field of the schema. Evidence turn "doc", quotes copied exactly; not_stated with empty values for anything the order does not show.
