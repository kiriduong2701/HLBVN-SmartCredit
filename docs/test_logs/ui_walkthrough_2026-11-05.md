# Browser walkthrough: 2026-11-05 (v2)

Headless Chrome 153 (Playwright) against a local server (`python3 run.py`). The scripts click the real UI; screenshots are in `docs/screenshots/`. These are automated walkthroughs. **No sessions with real users** have been run (see `peer_testing_kit.md`).

## Journeys executed

| # | Journey | Viewport | Result |
|---|---|---|---|
| 1 | Sign in → Home → Scan QR (ABC 12M) → Pay Later → plan picker (3 months shown unaffordable) → agree → face authentication → success | 1280×900 | Pass. Monthly 2,120,000 ₫ matches the server schedule |
| 2 | CUS-002: Pay Later → consent sheet (3 separate permissions) → AMBER → notice "review is off the counter" → Pay now → wrong Smart OTP rejected → correct OTP → paid | 1280×900 | Pass |
| 3 | Smart Credit hub → open request → upload 2 sample documents → submit → "Being reviewed" with expected time | 1280×900 | Pass |
| 4 | Officer: sign in (OFF-01) → queue → verify both documents → enter verified income → capacity recalculated → approve 15M with AP-04 (counter-offer) | 1366×900 | Pass |
| 5 | Customer: request shows "Approved 15,000,000 ₫" → purchase receipt offers "Convert to instalments" with per-tenure figures | 1280×900 | Pass |
| 6 | **Keyboard only**: Tab/Enter sign-in → Enter on QR → Space on "Pay now" radio → Enter → Enter → type OTP + Enter → success | 390×844 | Pass |
| 7 | English toggle on Home | 390×844 | Pass |
| 8 | After +40 days: Home shows a frozen banner; checkout disables Pay Later with a reason and a Pay loan link; loan shows the overdue instalment + late fee | 1280×900 | Pass |
| 9 | Presenter panel locked → unlock with code → controls visible outside the phone frame | 1280×900 | Pass |

## Automated checks

- Horizontal overflow: phone `scrollWidth` 390 = viewport. Officer 1280 / 1366 = viewport.
- Text smaller than 12px on the rendered phone screens: **none found**.
- Visible focus ring on buttons (`box-shadow: 0 0 0 3px #fff, 0 0 0 5px #2b4cc0`).
- Page JavaScript errors: **none**. Console shows only (a) Google Fonts blocked by the sandbox network, and (b) the intentional 422 from the wrong OTP.
- `node --check` passes for all five JS files.

## Issues found and fixed during the walkthrough

1. Toast notifications stacked above the authentication sheet and hid its error message. The modal now sits above toasts, and toasts last 3.5 s.
2. Officer workbench overflowed horizontally at 1280px (grid children without `min-width: 0`). Fixed. Two-column panels now stack below 1500px, so money values no longer break across lines.
3. Extracted document fields rendered one per line. They now render inline.
4. On phones, the floating *Presenter* button covered the Alerts/Profile tabs. Moved it above the tab bar (verified: button bottom 758 < tab bar top 774).
5. The frozen-line checkout lacked a direct route to pay the arrears. Added a *Pay loan* action.
6. Face-authentication threshold used "above 10M". The official HLBVN page says "10 million VND or more". Changed to inclusive, with a boundary test added.

## Not verified

- Real screen readers (VoiceOver/TalkBack). Semantics were implemented (native radios, labelled controls, `aria-hidden` icons, dialog focus trap, live region) but not tested with assistive technology.
- Real devices and Safari. Only Chromium was used.
- Vietnamese copy has not been reviewed by a native HLBVN copywriter or Legal.
