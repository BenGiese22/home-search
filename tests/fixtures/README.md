# Test fixtures

## Trimming rule: denylist only, never allowlist

`detail_*.json` are **real captured Compass payloads**, kept whole except for a
denylist of named bulky/PII keys (`fullContacts`, `history`,
`userListingCompliance`, `dealInfo`) and `media` truncated to 2 entries.

**Never trim a payload down to the fields the parser currently reads.** That is
exactly how a 164-leaf payload got certified as having "no structured HOA field"
on 2026-08-30: `canossa_dr_listing.json` (1.1 KB) and
`collection_response_sample.json` (1.3 KB) were hand-trimmed to the fields
`parse_listing_object` already consumed, so checking them for an unparsed field
could only ever confirm the parser's existing blind spots. The real payload is
~45 KB. See `docs/superpowers/plans/2026-08-30-structured-listing-fields.md`.

The two small legacy fixtures are kept because existing tests reference them.

## What each detail fixture is for

| Fixture | Address | Why it exists |
|---|---|---|
| `detail_hoa_association_yes.json` | 10191 Zenobia Circle | `Association: Yes`, `charges[chargeType==2]` = $1,000/yr, basement split populated |
| `detail_sfh_association_no.json` | 9313 West 91st Place | `Association: No`, no chargeType:2 entry — the confirmed-no-HOA signature |
| `detail_no_basement.json` | 8221 West 93rd Way | `belowGradeTotalAreaSquareFeet` absent entirely, `Basement: No` — absence means "no basement", not missing data |

Full raw captures for all 85 corpus listings live in `data/raw-captures/`
(gitignored) if a new fixture is needed.
