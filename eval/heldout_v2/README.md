# Held-out set v2

A second held-out set, written by a different author than the tuning items in
`eval/items/`. It is kept out of prompt writing and tuning: nobody working on the
reader prompts writes or reads it.

The author adds `*.yaml` EvalItem files here and, last, `MANIFEST.sha256` listing
**only** those `*.yaml` files (this README is exempt: it is never listed and never
loaded; any other file here, a symlink, or a manifest line outside this directory
makes the grader refuse), as `sha256sum`-style lines with repo-relative paths, e.g.
from the repo root:

    shasum -a 256 eval/heldout_v2/*.yaml > eval/heldout_v2/MANIFEST.sha256

`uv run python eval/run_eval.py --set heldout_v2` refuses to run until that manifest
exists and matches.

## Arguable labels

These fields have a defensible alternative reading. Each keeps the label below, for the
reason given:

- `hv2_read_patient_reply_03` `has_repair_number`: ambiguous. "I'm not sure ... maybe its in
  there?" is an uncertain answer; reading it as "no" is arguable.
- `hv2_read_visit_note_01` `criterion_d`: documented. The note states daily in-home use; the PT
  addendum (cannot self-propel) is about F, though reading D as contradictory is arguable.
- `hv2_read_pcp_reply_01` `send_by`: not_stated. The office declines to give a date; reading
  that refusal as ambiguous is arguable.
- `hv2_read_supplier_call_02` `k0001_in_stock`: found "no". The rep explicitly retracts the
  earlier yes ("Scratch what I said before"); contradictory is arguable.
- `hv2_check_safety_and_conditions_01` `other_condition`: absent. Dr. Chen waiting for the
  hospital's advice is a clinical hold, not a supplier condition.
- `hv2_check_safety_and_conditions_03` `needs_info`: absent. The ABN form and the delivery fee are
  the abn_requested and other_condition fields, not a request for her information.
- `hv2_read_pcp_reply_02` `item_confirmed`: not_stated. The office cannot ask the doctor; K0001
  appears only in our own question.

Grader limit, not a label choice: in `hv2_read_order_document_02`, `item_description` is graded
by containment, so a read that also includes the struck "Lightweight wheelchair" text still
matches "Standard wheelchair". It cannot tell struck text from corrected text; `hcpcs_code`
(exact match on K0001) carries that test.
