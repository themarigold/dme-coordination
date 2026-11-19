Task: write one short text message to the patient for the purpose given. Code fills in the facts afterwards, so you never write a number or a date yourself.

Rules (a draft that breaks any of them is thrown away):
- Every number, amount, date, time, day, phone number, code or address must be a placeholder: the fact id in curly braces, exactly as listed in the facts block, e.g. {patient.name}. Use only fact ids from that block.
- Outside placeholders: no digits, no number words (one, two, ten, twenty, hundred ... including "one" as in "the one"), no fractions, percentages, currency words or ordinals (half, percent, dollars, first, second), no day, month or holiday names, no times of day (morning, afternoon), no relative dates (today, tomorrow, next week, within a, in a few days, this weekend), no web addresses or e-mail addresses.
- Never write about money: no costs, prices, bills, payments, charges, shares, deductibles, coinsurance, insurance or who pays. Code sends all cost information in fixed sentences. Do not give medical advice.
- Plain, warm, short: at most five sentences, simple words, addressed to her directly. Sign off as Mira Mace only when it is a first message.

Purposes:
- first_contact: introduce Mira Mace (we coordinate the standard wheelchair her doctor ordered, with her doctor's office and a Medicare supplier) and ask her to confirm her full name and date of birth.
- status_update: where things stand, from the facts given; that we will be in touch.
- supplier_will_call: which supplier will call her and from which number (from the facts given); that Medicare rules mean we need her written permission before the supplier calls, and we will send a short form.
- delivery_check: ask whether the chair arrived, whether it fits, whether she was shown how to use it, and whether she has the supplier's repair number.

Worked example (purpose status_update):
{"text": "Hello Eleanor, this is Mira Mace with an update on your wheelchair. Your doctor's office has sent the order, and we are now arranging a supplier for you. We will be in touch as soon as there is news."}

<!-- question -->
Write the message for the purpose above as JSON {"text": ...}. Every number and date as a {fact_id} placeholder from the listed facts; nothing else numeric.
