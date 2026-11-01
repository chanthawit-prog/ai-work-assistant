# PRODUCT.md

Design brief for the dashboard, in the shape the [Impeccable](https://github.com/pbakaus/impeccable) design skill
reads (`/impeccable init` writes this file; it was written by hand here). Keep it current when the product changes.

## Product

**ผู้ช่วยงาน · AI Work Assistant** — a personal work assistant on a Synology NAS. You give Claude a task
(meeting summary, follow-up, sales brief, proposal draft, engineering question, or anything else), watch it work,
read the result, then approve it or tell Claude what to change. Processing runs on Claude with the owner's
subscription; the NAS stores every file and serves this dashboard. Recurring jobs (งานประจำ) repeat a request on a
weekly timetable, and engineering jobs can call a small calculation toolkit (pec_calc) whose every result carries its
formula, assumptions, references and the team's sign-off status. A full-text index of the knowledge folder (PDF, Word,
Excel, text) lets Claude and the user find the right page or row before reading, attached Excel files reach Claude as
CSV, approved results are also saved as Word in the library, and the dashboard can be added to a phone's home screen.

## Users and scene

One person: an engineer who also handles sales at a Thai engineering company. Mostly at an office desk on a laptop
or monitor in daylight; between meetings and on site visits, a phone in bright ambient light. Thai is the working
language, with English technical terms mixed in.

The scene decides the theme: **light by default**. Dark follows an explicit OS preference and is designed on its own
tokens, not inverted.

## Surface and direction

- **Mode: Operate.** The visitor is in a task. Scanability, consistency and standard controls come first; the brand
  lives in precise details.
- **Colour strategy: the company palette (PEC document tokens), Blue_650 as the one accent.** Blue_650 =
  `oklch(0.417 0.191 265.3)` = `#183BB2`, the OKLCH midpoint of PEC `BLUE_600 #1B43C8` and `BLUE_700 #15349C`.
  The header uses `BLUE_800 #122A7B`, the banner blue of PEC reports; neutrals are PEC greys (`GRAY_100 #F2F4F8`,
  `GRAY_700 #344054`), which already lean blue; job states use PEC red, green and amber. Blue_650 marks the primary
  action, the current selection, focus and the live state only. Screens, Word exports, reports and decks now share
  one palette.
- **Type: one family.** Anuphan, the PEC house font (Cadson Demak, self-hosted, SIL OFL), for Thai and Latin, on
  screen and in Word exports. It has no monospace cut, so code and file paths use the system monospace font with
  Anuphan filling in Thai. Fixed rem scale 13 / 14 / 16 / 18 / 26 px, line height 1.65 because Thai stacks marks
  above and below.
- **Density:** comfortable lists with dividers, no card grid.
- **Signature move:** every output document is shown as a **drawing sheet with a title block** (file, job, Rev.,
  last modified, path on the NAS). Each follow-up round with Claude, and each edit you save yourself, is a new
  revision, which is how engineers already track documents.

## Work areas

| Area | Job |
|---|---|
| Header | Where am I (งาน / ติดตามงาน), is Claude connected, how much subscription quota is used, is a job running |
| Home | One composer to send work to Claude, then jobs grouped by what needs you: รอคุณตรวจ, กำลังทำ, ล่าสุด |
| Job | Left: the conversation (your messages, Claude's replies, collapsible activity, live typing, your own edits) and the reply box. Right: the output sheet, which turns into a plain text editor (แก้ไขเอง) while the job waits for review |
| ติดตามงาน | Action items from approved meetings and ones you add, grouped by due date, tick to close, edit in a form above the list; a private calendar link puts due dates in the phone's calendar |

## Constraints

- Works offline on the LAN: no CDN fonts or scripts.
- Never collects Claude credentials; the setup help only explains Anthropic's own login steps.
- Word exports follow the same palette and Anuphan at PEC's report sizes (body 10 pt, tables 9 pt), with the font
  embedded so recipients without it see the same document; a company template can replace the styles.
- Runs on a NAS with little RAM (DS925+, 6 GB, swap fills easily): one job at a time, jobs wait for free RAM, the
  container is capped and kept out of swap, and every run records its peak memory.
- WCAG AA: body and placeholder text ≥ 4.5:1, control boundaries and focus ≥ 3:1 (checked in both themes).
- Touch targets ≥ 44 px on coarse pointers; no horizontal scroll at 390 px.
- Motion only for state (running indicator, toast, skeleton); a reduced-motion path keeps fades and drops loops.

## Anti-references

Impeccable's refusals that this design is checked against: card grids as page structure, nested cards, eyebrow
labels above headings, modals for non-blocking tasks, gradient text, glass/blur, coloured side stripes, emoji as
icons, monospace as decoration, a grey screen with one accent and nothing else.
