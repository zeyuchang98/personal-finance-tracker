# Categorization Guide

This file steers how Claude categorizes merchants that aren't already in the
rule library. Edit it freely — plain English. It is injected into every
classification request, so changes take effect on the next sync. No code changes
needed.

Keep it short and concrete. Claude still must pick from the app's current
category list (base + your custom ones); this guide only nudges *which* one.

EVERY transaction goes through Claude — spending and money movement alike. Each
line gives Claude the merchant name plus, in brackets, the direction (money OUT =
a charge you paid, money IN = a deposit/credit), the amount, the account type,
and Plaid's category hint. Classify by the merchant name first; use the amount as
a secondary signal when a rule below calls for it. Besides spending categories,
the label list includes money-movement labels (Salary, Other Income, Transfer,
Investment, CC Payment, Cash, Refund) — see their meanings below. (Merchants already in the
rule library get their fixed label regardless of amount.)

## Category meanings

- Rent: housing rent or mortgage payments.
- Utility: electricity, water, gas, internet, phone bills.
- Shopping: general retail, electronics, clothing, household goods.
- Dining: restaurants, cafes, bars, food delivery, coffee shops.
- Grocery: supermarkets and grocery stores (food to cook at home).
- Subscription: recurring digital services (streaming, software, memberships).
- Medical: pharmacy, doctor, dental, clinics, health — humans only (animal
  care goes to Pets).
- Transport: gas, fuel, rideshare, transit, parking, tolls, flights.
- Pets: anything pet-related — vets and animal hospitals, pet insurance, pet
  stores and supplies (Chewy, Petco, PetSmart), grooming, boarding.
- Tax: tax payments — IRS, state tax authorities (e.g. NY DTF, CA FTB),
  estimated tax, tax-prep fees paid with a filing (TurboTax, H&R Block).
- Other: a spending charge that doesn't clearly fit above.

Money-movement labels (NOT spending — these are excluded from spending totals,
except Refund which offsets it):

- Salary: employment income — regular payroll / paycheck / direct deposit from
  an employer (PAYROLL, DIRECT DEP, SALARY, PAYCHECK, or a payroll processor like
  ADP / Gusto / Paychex, or the employer's own name). Usually recurring and a
  similar amount each time. Money IN. Not spending.
- Other Income: any OTHER money coming IN that isn't salary — interest,
  dividends, tax refunds, cashback/rewards redemptions, rebates, gifts, one-off
  deposits, and unrecognized inflows you can't attribute to a refund or a
  reimbursement. Money IN on a checking/savings account. Not spending.
- Transfer: moving your OWN money between your accounts (the word TRANSFER, or
  Plaid TRANSFER_IN / TRANSFER_OUT). Not spending.
- Investment: contributions to brokerage/retirement accounts — Vanguard,
  Fidelity, Schwab, Robinhood, E*Trade, Wealthfront, Betterment, Coinbase, or
  Plaid INVESTMENT_AND_RETIREMENT_FUNDS. Money moved, not spent.
- CC Payment: paying your credit-card bill (PAYMENT THANK YOU, AUTOPAY,
  CARDMEMBER, CARD PMT, or Plaid CREDIT_CARD_PAYMENT). The card purchases are the
  real spending, so the payment itself is excluded.
- Cash: ATM / cash WITHDRAWALS only — money OUT (ATM, CASH WITHDRAWAL). Cash
  spending is untracked. NOTE: a cash DEPOSIT (money IN, e.g. "CASH DEPOSIT",
  "ATM DEPOSIT") is NOT Cash — default it to Other Income (new money coming in).
  Only use Cash for a deposit if the user is clearly re-depositing cash they
  earlier withdrew.
- Refund: a merchant returning money for a PRIOR purchase (money IN with REFUND /
  RETURN / REVERSAL, or a non-payment credit on a credit-card account). Offsets
  spending.

Note on P2P (Venmo, Zelle, Cash App, PayPal, Apple Cash): NEVER default these to
Transfer, even when Plaid tags the row TRANSFER_OUT / TRANSFER_IN. Classify by
direction. Money OUT to a person is usually a real expense — pick the spending
category it was for (e.g. splitting dinner → Dining; Other if unclear).
- P2P money IN (Zelle, Venmo, Cash App, Apple Cash): default to the Reimbursement
  category — these are almost always friends settling up / paying you back for a
  shared outing. ONLY override to Salary or Other Income when it's clearly real
  income (a roommate's rent, payment for work, payroll).
- Reimbursement: money paid BACK to you — friends settling up, expense
  reimbursements, insurance payouts. Money IN that cancels a prior expense; the
  app records it as a NEGATIVE offset that reduces your spending total.

## Special cases / preferences

- Amazon: default to Shopping. If the charge clearly references Prime, treat as
  Subscription. As an amount tiebreaker for plain "Amazon" charges, a small
  recurring-sized charge (under ~$20) is likely a Subscription (Prime, Kindle,
  Audible); larger charges are Shopping.
- Payment aggregators (PayPal, Square "SQ", Toast "TST"): the real merchant is
  usually the name after the asterisk — classify by that real merchant, not the
  aggregator.
- Warehouse clubs (Costco, Sam's Club, BJ's): treat as Grocery unless clearly not food.
- Target / Walmart: default to Shopping (mixed retail); use Grocery only if clearly groceries.
- Coffee shops (Starbucks, etc.): Dining.
- Gas stations: Transport.
- Credit-card internal balance moves are NOT spending → Transfer. This includes
  "OFFER … MOVED TO STANDARD PURCH", "OFFER … PROMOTIONAL APR ENDED", and similar
  promo/installment-bucket reclassifications (the real purchase already happened;
  these net to zero on the card).

## Personal rules

Your own generalizations, added when you correct a transaction and pick "Teach
Claude a pattern". Edit or delete these freely.
