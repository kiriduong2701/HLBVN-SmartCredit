# Peer / customer testing kit (not yet run with real people)

The prototype has been validated with **mock personas** (automated logs in `docs/test_logs/`) and a scripted UI walkthrough by the build agent. It has **not** been tested with peers or target borrowers yet. Use this kit to run and record those sessions so the results are verifiable.

## Setup (per session, ~20 min)
1. `python3 run.py --reset` on the facilitator laptop and open `http://127.0.0.1:8080` in a phone-sized browser window. Open `/officer.html` (opens directly, no code) and `/navigation.html` (scenarios, simulated date, outages) on a second screen for the facilitator only; participants see just the customer app.
2. Tell participants that everything is fictional and simulated, and that they should never enter real banking credentials.
3. Record: participant ID (P01…), segment (peer / retail borrower / gig worker / first-time borrower), device, date.

## Tasks (read aloud, don't help unless stuck > 60 s)
| Task | Persona | Success = | Observe |
|---|---|---|---|
| T1 Buy the smartphone with Pay Later | Ta My Kim Tien → ABC Electronics 12M | Picks an affordable plan, passes face check, reaches "Payment successful" | Do they compare monthly, total and interest? Do they understand why 3 months is unavailable? |
| T2 Find your first due date and pay it | same | Pays instalment 1 in My loans | Can they find Pay loan from Home? |
| T3 Buy the air purifier with Pay Later | Vo Minh Khoa → Song Lam 4.5M | Gives consents, understands the limit is in review **off the counter**, pays now | Do they feel rejected? Is "pay now, convert later" clear? |
| T4 Activate Smart Credit from Loans & Cards | Dang Thu Ha | Makes an informed choice for each alternative-data source (all unticked by default) | Do they understand why more data is needed, what is shared, and that "no" is OK? |
| T5 Buy the electric scooter | Ta My Kim Tien → Mekong 30M | Understands it's above the available limit, and chooses split tender (Pay Later part + Pay Now rest), Pay Now or the limit-increase request | Is it clear that the split is a choice and that Pay Now stays available? |
| T6 Switch the app to English and back | any | Finds the VI/EN toggle | Wording quality in both languages |

## Record per task
| Participant | Task | Completed (Y/N) | Time (s) | Errors / wrong taps | Confidence 1–5 | Quote | Facilitator note |
|---|---|---|---|---|---|---|---|

## Post-session questions (1–5 Likert + why)
1. I understood when my purchase was approved versus actually paid.
2. I understood why extra data was requested and what each source would share.
3. I felt in control of what data was shared.
4. I would use QR Pay Later for a purchase like this.
5. The app looked and felt like HLB Connect.

## Evidence to keep
- The filled tables above (anonymised), plus the server's audit export: sign in as an officer, then `curl -H "Authorization: Bearer <officer token>" http://127.0.0.1:8080/api/officer/audit > session_P01_audit.json` (see docs/API.md).
- Don't record real names, phone numbers or account details.
