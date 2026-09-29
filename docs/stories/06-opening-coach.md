# Story 6: Opening coach — Jobava London & Caro-Kann

**Status:** Done
**Depends on:** Story 3 (games), Story 4 (analysis results)
**Commands introduced:** `chess-analysis report-openings [--output DIR]`

## Goal

Improve the two openings I actually play: the **Jobava London** (1.d4 2.Nc3 3.Bf4, White) and the **Caro-Kann** (Black vs 1.e4). Know my lines, know where I deviate, know where opponents deviate, and where those deviations are punished.

## Tasks

- Opening detection
  - Classify my games as `jobava_london` (White, by my move sequence) and `caro_kann` (Black, 1...c6 vs 1.e4 — all anti-Caro lines included: Advance, Exchange, Fantasy, side lines)
  - Games that *should* have been the opening but where I (White: setup deviation) or the opponent avoided it are tracked separately ("avoided" bucket)
- Line tracking from my own games
  - For each opening: which variations actually occur, with move-sequence keys (first 10–12 plies)
  - Per variation: W/L/D, average accuracy both sides, move where the first blunder/inaccuracy happens (mine and opponent's), where I leave "my" setup
- Reference book
  - Build a small opening book from free data: lichess opening explorer (Masters / Lichess DB, public API or bulk download) for both openings
  - For every game: detect the ply where the game leaves the reference book (for me and for the opponent), and whether the leaver got punished (eval swing within the next few moves)
  - Book stored in SQLite (`opening_book` table, keyed by FEN prefix/move sequence) so reports are offline
- Per-opening drill-down (HTML, same self-contained pattern as Story 5; output to bind-mounted `./reports/`)
  - Jobava London: my setup consistency, most common White deviations, opponent's best-scoring anti-Jobava lines and where they punish me
  - Caro-Kann: variation coverage per anti-Caro line, my first inaccuracy move distribution per line, which opponent deviations I fail to exploit
- Repertoire gaps: variations I have never (or rarely) faced get listed with their reference-book mainline so I can prep

## Acceptance criteria

- [ ] All my games are classified: jobava_london / caro_kann / other / avoided
- [ ] Per variation: results, accuracy, first-blunder move, and book-leaving ply are reported
- [ ] I can see which specific lines in the Jobava London and in each anti-Caro line cost me the most accuracy
- [ ] Opponent deviations I failed to punish are listed with examples
- [ ] Unfaced-but-relevant variations are listed with a reference mainline
- [ ] Report opens offline in a browser

## Notes

- White's Jobava London is a *system* opening: the interesting question is setup consistency and how opponents disrupt it. The Caro-Kann is the opposite — White chooses the variation, so coverage across all anti-Caro lines matters.
- lichess opening explorer data is public and free; respect its rate limits when building the book, then work offline.
- Possible follow-up (out of scope): spaced-repetition practice of flagged lines.
