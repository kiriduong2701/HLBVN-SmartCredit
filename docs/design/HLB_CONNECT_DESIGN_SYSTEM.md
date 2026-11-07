# HLB Connect design analysis and Smart Credit design system

**Evidence:** 14 screenshots of the live HLB Connect Vietnam iOS app (v2.5.184, captured 5–6 Nov 2026). They show login, home, the quick-links menu, the side drawer (collapsed, scrolled and expanded), account detail, Send Money, recipient form, Top up, Pay bills, Open Fixed Deposit, the Personal Loan landing page and Scan QR. Colours were **sampled from the screenshot pixels** (5×5 averages), not estimated by eye. Sizes are converted at the device scale (1179 px ÷ 3 = 393 pt).

The brief mentioned a suggested colour palette and typography reference, but **none was attached**, so there was no second source to validate against. Where the screenshots don't show something, it's marked *not evidenced* and handled with neutral design judgement.

## 1. Principles identified

| # | Principle | Evidence |
|---|---|---|
| P1 | **One immersive brand backdrop.** A blue-tinted skyscraper photograph sits behind *every* customer screen: login, home, every flow. It runs from cyan-blue at the top (`#4EA4C4`) through `#4885AE` and `#2D4F6D` to deep navy (`#052847`–`#1F3A52`). There are no plain white page backgrounds, except the lower half of account detail | All 14 screens |
| P2 | **Two surface types with distinct jobs.** (a) *Glass tiles* on home: white at about 20% over the backdrop, with white text, for at-a-glance products. (b) *Solid white cards* for tasks (forms, lists, grids, the login box), with grey text | Home vs Send Money, Top up, Pay bills, login |
| P3 | **A single action colour.** Every primary button, list icon, avatar and checkbox is the same blue `#3E7CF3` (Next is `#4D7AEB`, a rendering variant). Buttons are full-width **pills** with medium-weight white text | Login, Next, Top up, Apply now; list icons; drawer avatar |
| P4 | **Red and navy belong to the logo only.** The HLB red and navy never appear as UI colours | All screens |
| P5 | **A categorical accent palette, used only in circular icons.** Teal `#5ECCD4`, light blue `#70B7FF`, pink `#E877A3` (Quick links, Pay bills), orange `#FF9F5D`. One featured product gets a **diagonal gradient tile** `#4167B2 → #27C4EB` (Personal Loan) | Home, account detail, quick links, loan landing |
| P6 | **Transparent header.** White line icons, centred white title (~18 pt medium), back chevron on the left, optional info icon on the right. Home replaces this with hamburger + QR + bell | Every inner screen |
| P7 | **No bottom tab bar.** Navigation is a left **drawer** (profile, settings, messages, logout), **QR and bell** in the header, and a pink **Quick links** button that opens a radial shortcut menu over a dark scrim | Home, quick links, drawer |
| P8 | **Question-style section headings.** A centred white heading above each card: "Where are you sending money to?", "How much do you want to top up?" | Send Money, Top up, Pay bills |
| P9 | **Underline inputs.** Each field has a blue outline leading icon and a hairline underline. Small uppercase white labels (FROM / TO) sit on the backdrop. Key computed values are cyan `#4FD8E8`-like on dark backgrounds | Recipient info, Fixed Deposit, login |
| P10 | **Calm, low-density layout.** About 16 pt side margins, card radius about 12 pt, list rows about 44 pt with `#F0F0F0` hairlines and grey chevrons `#AFB5C3`, generous vertical space, few elements per screen | Lists, grids |
| P11 | **System typography.** iOS San Francisco, mostly regular weight; medium for titles and buttons; semibold for featured-card titles. Customer names are uppercase. Empty states and "Last login" are italic grey | All |
| P12 | **Flat elevation.** Cards have no or very faint shadows (the account action panel has a soft one). Hierarchy comes from surface contrast, not depth | All |

**How screen types differ.** *Login*: logo lockup, "Hello, NAME", one white card (password + pill + magenta face icon), campaign mark. *Home*: greeting, masked balance pill with eye toggle, stacked glass product tiles, Quick links button. *Product entry (Personal Loan)*: round logo badge, illustrated promo card, three feature rows with coloured circles, pill CTA, underlined T&C link. *Task flows*: transparent header, question heading, white card(s), pill CTA inside the card. *Scanner*: darker scrim, VietQR | HLB Connect lockup, green corner frame `#64B12A`, NAPAS 247 mark, dark bottom bar.

## 2. Design tokens (implemented in `web/css/app.css`)

| Token | Value | Source |
|---|---|---|
| `--hlb-blue` (primary action) | `#3E7CF3` | Sampled: Login, Apply now, avatar, list icons |
| `--hlb-blue-press` | `#2F68D8` | Derived (pressed state; *not evidenced*) |
| `--bg-1 … --bg-5` (backdrop stops) | `#4EA4C4 · #4885AE · #2D4F6D · #1F3A52 · #052847` | Sampled from backdrop |
| `--glass` / `--glass-strong` | `rgba(255,255,255,.18)` / `.26` | Matches sampled tile `#4F89B0` over `#2D4F6D` |
| `--accent-teal / -lblue / -pink / -orange` | `#5ECCD4 · #70B7FF · #E877A3 · #FF9F5D` | Sampled icon circles |
| `--promo-grad` | `linear-gradient(100deg,#4167B2,#27C4EB)` | Sampled Personal Loan tile ends |
| `--text-strong / --text / --text-muted / --text-hint` | `#3A3D42 · #54565B · #737880 · #A9AFBC` | Sampled values, drawer text, placeholders |
| `--line` / `--chevron` | `#E9ECEF` / `#AFB5C3` | Sampled hairline / chevron |
| `--tint` | `#EFF3FE` | Sampled recipient header band |
| `--drawer-grad` | `#EDF7FD → #EDF1F4` | Sampled drawer |
| `--scan-green` | `#64B12A` | Sampled scanner frame |
| `--value-cyan` | `#5FD6E6` | Fixed Deposit computed values |
| Semantic `--ok / --warn / --err` | `#1E8E5A · #B26A00 · #D2384A` (with tints) | **Not evidenced** in the screenshots. Needed for credit statuses; kept desaturated, used only on chips and status icons, never for actions |
| Font | `"Noto Sans"`, then the system stack | **v7:** Noto Sans is the typeface on the "Brand assets" page of the HLBVN Gala Round case (p. 12), which is an official source and outranks the app screenshots. Self-hosted (`web/fonts/`, SIL OFL) so the pitch works offline. The app screenshots showed the iOS system font, which remains the fallback |
| Type scale | title 18/600 · body 16/400 · secondary 14/400 · caption 12/400 · amount 24–28/600 | Converted from the screenshots |
| Radii | card 12 · tile 12 · input 0 (underline) · button 999 · icon circle 50% | Screenshots |
| Spacing | 4-pt grid; screen gutter 16; card padding 16–20; row 48 min | Screenshots |
| Elevation | none on glass; `0 2px 10px rgba(16,40,70,.08)` on white panels | Account action panel |

**Decisions that differ from the previous MVP v2:**
- Red primary buttons → blue `#3E7CF3` (P3, P4).
- Navy and red gradient cards → glass tiles plus one promo gradient (P2, P5).
- Be Vietnam Pro (Google Fonts) → system font (P11). This also removes an external dependency.
- 5-tab bottom bar → drawer + header QR/bell + Quick links (P7).
- Boxed inputs → underline inputs where they are form fields (P9).

## 3. How Smart Credit maps onto the patterns

| Smart Credit element | HLB pattern reused |
|---|---|
| Home entry | A **featured gradient tile** in the product stack (same slot type as Personal Loan), showing available limit or "Mua trước, trả sau" |
| Product entry / activation | **Product-landing template** (logo badge, promo card, three feature rows with teal / pink / orange circles, pill CTA, underlined terms link) |
| QR payment | **Scanner template** (scrim, VietQR \| HLB Connect lockup, green frame). Pay Later appears as a payment option on the payment confirmation card |
| Pay Later confirmation, plan picker, consents | White task cards with a question heading, pill CTA inside the card, consent checkboxes like Fixed Deposit |
| Repay instalments | Reached from **Quick links** (like *Pay bills → Installment payment*) and from the tile |
| Drawer | Adds Smart Credit, My loans, Transactions and Language next to profile-style rows. Logout and version in the footer, as in the app |
| Smart OTP / face | Bottom sheet; the face icon uses the login face-ID styling |

## 4. Officer workbench

Same tokens, font and blue action colour. The navy backdrop stop `#052847` becomes the app bar, and white 12-pt panels sit on the drawer neutral `#EDF1F4`. No backdrop photo, no glass, no Quick links. Semantic colours are reserved for risk and decision states.

**v7 layout (for non-technical credit officers).** The official horizontal HLB logo from the case's Brand assets page sits on a white plate in the app bar (the logo's own navy and red stay inside the logo, P4). Each case has two tabs:
- *Review & decide*: (1) why the case needs you, in plain words with the rule code as a small tag; (2) key figures as tiles (most you can approve, automatic limit, requested, income, low-income month, debt payments, DTI) and what sets the limit; (3) customer profile and risk signals against their thresholds; (4) data checks, issues first; (5) consent and data sources; (6) decision history as a timeline. The decision form sits in a sticky right column (after the key figures below 1280 px): eligibility in one line, authority and allowed range, Approve / Reject (/ Fraud) as a segmented control, reason codes filtered to those the server will accept (EL-10), inline validation.
- *Audit & technical*: the append-only audit log with raw JSON details, the decision trace (rule ids, policy version and hash, engine time, Layer 0 checks), evidence lineage, the full capacity calculation, the API call log and governance. Nothing from the previous screen was removed; technical items moved here.

## 5. Not verified / not reproduced

- **The skyscraper photograph** is HLBVN's asset and was **not copied**. A CSS gradient with abstract vertical "facade" bands approximates its colour and mood.
- The **HLB Connect wordmark, #KKCT campaign mark, VietQR, NAPAS and DIV logos** were not redrawn. The team-supplied app icon and plain-text labels are used instead.
- The **icon set** is a generic line/filled set drawn to match the style, not HLBVN's icons.
- Android rendering, dark mode, motion and transitions, Vietnamese-language screens and error/alert dialogs are **not evidenced** in the screenshots.
- Exact font metrics are inferred from iOS rendering. No official HLBVN design specification was available, so this is **not a pixel-perfect or brand-approved match**.
