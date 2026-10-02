# Prospective triggers: which phrasing or decision rule actually separates?

Run date 2026-09-20 · corpus version 1 · `bge-m3`, 1024 dimensions · Palinode 0.21.0
Raw output: [`results.json`](results.json) · full generated tables: [`tables.md`](tables.md)
Rig: `python -m bench.trigger_phrasing`

Prospective triggers do not fire in practice. The observed reason is that real
prompts score well below the 0.70–0.75 default threshold against the trigger
they are genuinely about, while off-target prompts reach into the same band — so
lowering the threshold alone trades silence for noise. Four options were
proposed and none chosen; this is the labelled measurement that was asked for
before choosing.

## What was measured

A synthetic, versioned corpus (`bench/trigger_phrasing.yaml`): **14 triggers**,
each written three ways — the third-person `docs_style` the tool descriptions
currently teach ("User is asking how to…"), the same intent `user_worded`, and
3–4 user-worded `phrasings` scored on their max — and **84 prompts**, stratified
per trigger into on-target terse (28), on-target verbose/session-handoff-shaped
(28), near-miss off-target (14) and unrelated off-target (14).

Every prompt is scored against every trigger: **1176 labelled pairs, 56 of them
on-target**. A pair is positive only when the prompt is on-target *and* the
trigger is the one it is about; an on-target prompt scored against a different
trigger is a negative, because firing there is a false delivery.

Scores come from the product: `embedder.embed` for every text, real
`store.add_trigger` / `store.check_triggers` against a throwaway SQLite store,
so the vec0 distance and the shipped `1 − d²/2` conversion are the ones under
test. The lexical arm is a real FTS5 index over the same description text,
queried with the product's own `fts_match_expression` and its BM25
normalization. No production default was changed and no vector was synthesised.

## Headline numbers

Separation over all 1176 pairs (AUC = P(on-target pair outscores off-target
pair); 0.5 is chance):

| Arm | On-target median | Off-target median | AUC | Best-F1 threshold | Precision | Recall |
|---|---:|---:|---:|---:|---:|---:|
| `docs_style` (today) | 0.593 | 0.461 | 0.893 | 0.585 | 0.516 | 0.571 |
| `user_worded` (option 1) | 0.554 | 0.434 | 0.897 | 0.590 | 0.743 | 0.464 |
| `phrasings_max` (option 2) | 0.664 | 0.480 | **0.951** | 0.647 | 0.816 | 0.554 |

At the shipped default, the channel is dead on this corpus too: `docs_style` at
0.75 delivers **3/56** on-target pairs (5.4%), at 0.70 **7/56** (12.5%). That is
with clean synthetic prompts; the live sample in the issue is worse.

**Option 1 (user-worded descriptions) does not move separation** — AUC 0.897 vs
0.893 — but it does shift the operating point toward precision: at 0.50,
`docs_style` fires on 294/1120 off-target pairs and 67/84 prompts pick up a
trigger that is not theirs, against 135/1120 and 55/84 for `user_worded`, at
roughly the same recall (49/56 vs 46/56). Cheaper, slightly safer, not a fix.

**Option 2 (several phrasings, fire on max) is the largest single win.** AUC
0.951, best-F1 0.660 against 0.542/0.571 for the single-description arms, and
the whole on-target distribution moves up (terse median 0.767 against 0.620).
At a matched prompt-level false-fire rate it dominates: `phrasings_max` at 0.65
delivers 29/56 (51.8%) with 6/84 prompts false-firing, where `docs_style` at
0.60 delivers 26/56 (46.4%) with 11/84.

**Option 3 (a lexical arm) is a cheap recall add-on, not a separator.** BM25
alone over trigger text: AUC 0.740 (`docs_style`), 0.895 (`phrasings_max`), and
its scores are near-zero for most pairs (off-target median 0.000). ORed with the
vector arm at a 0.20 BM25 floor it is close to free at high vector thresholds —
`docs_style` at 0.70 goes 7/56 → 12/56 on-target with off-target pairs
unchanged at 1/1120 — but on `phrasings_max` at 0.70 it costs 2/1120 → 15/1120
off-target for 22/56 → 31/56 on-target. It buys recall where the vector arm has
already given up; it does not resolve the overlap band.

**Option 4 (a relative rule) is the best decision rule measured.** A *pure*
relative floor is degenerate — the best trigger always clears a fraction of
itself, so every prompt fires (margin 0.00: 2/84 silent, 35/84 prompts
false-fire on `phrasings_max`). The usable form is: one winner per prompt,
gated on a low absolute floor (0.40) *and* on its lead over the runner-up. At
margin 0.06 with `phrasings_max`: **34/56 correct deliveries (60.7%) with 2/84
prompts false-firing (2.4%)**. The absolute-threshold arm needs 0.70 to reach
the same 2.4% false-fire rate, and delivers only 22/56 (39.3%) there. Same
noise budget, +21 points of delivery.

Combination table, prompt-level, all denominators explicit:

| Configuration | Correct deliveries | Prompts with a false fire |
|---|---|---|
| `docs_style`, absolute 0.75 (shipped default) | 3/56 (5.4%) | 0/84 (0.0%) |
| `docs_style`, absolute 0.60 | 26/56 (46.4%) | 11/84 (13.1%) |
| `user_worded`, absolute 0.60 | 22/56 (39.3%) | 9/84 (10.7%) |
| `phrasings_max`, absolute 0.65 | 29/56 (51.8%) | 6/84 (7.1%) |
| `phrasings_max`, absolute 0.70 | 22/56 (39.3%) | 2/84 (2.4%) |
| `docs_style`, relative floor 0.40 + margin 0.06 | 27/56 (48.2%) | 4/84 (4.8%) |
| `user_worded`, relative floor 0.40 + margin 0.06 | 24/56 (42.9%) | 6/84 (7.1%) |
| **`phrasings_max`, relative floor 0.40 + margin 0.06** | **34/56 (60.7%)** | **2/84 (2.4%)** |
| `phrasings_max`, relative floor 0.40 + margin 0.08 | 33/56 (58.9%) | 1/84 (1.2%) |

## What this corpus cannot tell you

- **It is synthetic and kinder than production.** Its on-target prompts are
  single-topic by construction; the live sample in the issue scored 0.42–0.55
  where `docs_style` here has an on-target median of 0.593. Read the *ordering*
  of the options as transferable and the *absolute* numbers as optimistic. Any
  number chosen as a shipped default must be re-checked against real prompts.
- **56 positives.** Enough to rank four options, not enough to calibrate a
  threshold to two decimal places.
- **One embedding model.** `bge-m3` only. The conclusions are about this
  model's geometry.
- **The relative rule was measured on vector scores only.** Combining the lead
  margin with the lexical arm was not measured — the two arms are on
  incomparable scales, so a lead in one is not a lead in the other.
- **Verbose prompts stay hard.** Even in the best arm, on-target verbose
  prompts median 0.586 against 0.767 terse. A session-handoff turn dilutes the
  matching intent no matter how the trigger is written; nothing measured here
  fixes that, and a per-sentence or per-segment match is the untested option.

## RECOMMENDATION

For the maintainer to decide; none of it is implemented in the change that
carries this report.

1. **Take options 2 and 4 together; treat option 1 as documentation only.**
   Multi-phrasing trigger registration (fire on the best phrasing) plus a
   relative rule — one winner per prompt, absolute floor 0.40, lead over the
   runner-up at least 0.06 — is the only configuration measured that delivers
   the majority of on-target prompts at a low single-digit false-fire rate.
   Option 1 is worth doing anyway because it costs a docs and tool-description
   edit, but on its own it is not the fix: it changes precision, not separation.
2. **Do not ship a lower absolute threshold on its own.** Every absolute floor
   that delivers more than half the on-target prompts also fires a wrong trigger
   on 7–22% of prompts. Lowering 0.75 to 0.50 without a relative rule converts a
   dead channel into a noisy one.
3. **Defaults, if this lands:** keep per-trigger `threshold` as the absolute
   floor but re-default it to **0.40** (the floor's job becomes "is this
   remotely related", not "is this the answer"), and add a store-level margin of
   **0.06**. Re-validate both on a real prompt sample before the release that
   changes them — the synthetic corpus is optimistic.
4. **Existing triggers' stored thresholds must be migrated, not respected.**
   Every trigger registered so far carries 0.70–0.75 in its row, and those rows
   are DB-only state with no re-embed path. Leaving them alone means the fix
   ships and nothing changes for any trigger that exists today. The minimum is a
   one-shot migration that rewrites any stored threshold at or above 0.65 to the
   new default, announced in the changelog; the better version also re-registers
   the descriptions once multi-phrasing exists, since a trigger written in the
   old third-person style keeps the lower distribution.
5. **Yes to a `doctor` check.** "N triggers registered, none fired in X days" is
   exactly the signal that was missing for six months, and it is cheap:
   `fire_count` and `created_at` are already in the row. Suggested shape: WARN
   when a store has at least 3 enabled triggers, all older than 30 days, with a
   total `fire_count` of 0. It should stay advisory — a store whose triggers are
   all genuinely narrow can legitimately be quiet — but it must be visible,
   because the failure mode of this feature is silence, and silence is also what
   it looks like when it is working.
6. **Re-run this rig against any change.** It is 14 triggers and about 170 embed
   calls; the whole run takes under a minute against a warm endpoint, and the
   corpus is versioned so a later run is comparable.
