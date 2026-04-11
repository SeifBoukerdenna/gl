---
name: User Preferences and Working Style
description: How to collaborate with this user — response style, decision-making patterns, terminology, and consistent feedback about what works
type: user
---

## Response style

- **Short and precise over long and thorough.** User has explicitly said multiple times "concise and precise is better than long responses" and "stop summarizing what you just did." Lead with the answer, skip preamble.
- **Concrete tables and numbers over prose.** When there are metrics to show, use a table. When there's math, show it.
- **No emojis unless they ask.** They never have.
- **Don't restate the question.** Just answer.
- **Do not use "I'll continue" or recap preambles.** Resume directly.

## Decision-making patterns the user uses

- **Explores before committing.** Often asks "what can we do now?" or "what else?" rather than committing to a single action. Present a menu with a recommended option.
- **Wants the reasoning exposed.** Not "do X" — "here's why X vs Y, I recommend X, but tell me what you want."
- **Correctsme when I'm wrong.** They caught me multiple times: wrong about oracle_chrono being a "lucky outlier" when the rolling blocks showed it wasn't, wrong about the halt reasoning. They're watching. When wrong, own it immediately, explain the mistake, don't rationalize.
- **Values data preservation over protection.** Explicitly said "we're doing a full-on exploration run, let sessions run without interruption, post-session analysis can simulate what would have happened with a stop." This is the RIGHT approach for the current phase.
- **Wants to see the workings of suggestions.** When I suggested the tiered halt, they asked "but how do we distinguish a bad patch from a bloodbath" — they want the edge-case thinking exposed, not hidden.
- **Prefers per-session tuning over global config.** The halt-sweep finding (each session has its own optimum) resonated strongly.

## Terminology they use

- **"bloodbath" / "bloodbath proof"** — regime shift causing portfolio-wide losses. Specifically refers to the Apr 9 event where oracle went $23k → $8k in 5 hours.
- **"chop"** — BTC oscillating around the strike without trending. The enemy of all directional strategies.
- **"nuke"** — delete trades.csv and restart a session with clean data. Used routinely after config changes.
- **"tier"** (as in "tier 1/2/3/4 trust") — from the viability framework table (trade counts + regime diversity).
- **"post-hoc"** — analyzing historical data with a new filter/overlay after the fact.
- **"exploration mode" vs "live"** — current phase is exploration (paper, no real money). Live means real capital deployment.

## User context (what I know about them)

- Technical background — can read Python, understand halt thresholds, correlation matrices, statistical significance
- Project: Polymarket 5-min BTC binary prediction bot. Paper trading phase, preparing for live deployment eventually.
- Dashboard on local Mac + Cloudflare tunnel for phone access
- Runs the VPS (`root@167.172.50.38`) and the bot sessions on it
- Has been working on this bot project for weeks — it's a substantial ongoing effort
- Values statistical rigor (asked about sample sizes, confidence intervals, regime tests)
- Is risk-aware but willing to take calculated experimental risks
- Prefers structured decision-making (menus, tier systems, explicit criteria)

## Things that work

- **Menu + recommendation** format for "what should we do next" questions. Give 3-4 options, rank them, pick one, explain why, ask what they want.
- **Explicit caveats** — when something is overfit or has tiny sample, say so loudly. User has internalized this and asks "is this enough data" on their own now.
- **Math walkthroughs** — when explaining a mechanism (e.g., how the 0.95 correlation can coexist with 3× PnL difference), show the actual numbers.
- **Honest uncertainty** — "I don't know" or "this might be noise" is better than false confidence.
- **Tables** — the user processes dense tabular data well.

## Things that don't work

- **Long preambles** — user skips them and gets annoyed
- **Hedging every statement** — be decisive when possible, caveat when necessary
- **Pretending to be certain when I'm not** — user catches this
- **Reusing names across contexts** — I made oracle_lazy a "10-second fixed wait" and the user rightly said "do something more ingenious." Avoid lazy naming and lazy mechanism design.
- **Not running smoke tests after deployments** — user pointed out I keep introducing bugs in features I add because I only check `systemctl is-active`, not actual signal processing
- **Suggesting live changes during exploration mode** — user has clearly stated the exploration-first philosophy
