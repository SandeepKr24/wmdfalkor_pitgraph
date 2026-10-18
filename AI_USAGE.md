# AI Usage Disclosure

A log of what the AI assistant (Claude Code) did during the build.

## Phase 1 — Oct 15, 2026

- Created `./AI_USAGE.md` to document all AI-assisted actions throughout development.
- Set up a Python 3.11 virtual environment with `uv` and pinned the required dependencies in `requirements.txt`.
- Verified FalkorDB's algorithm syntax (PageRank, label propagation, WCC, SPpaths), and tested each one on a temp graph.
- Proposed various designs for T6 (label propagation over derived `Circuit-FAVOURS->Strategy` edges)
  and T7 (PageRank over `Circuit-SIMILAR_TO->Circuit`), which Sandeep (the developer) decided upon.
- Verified the loaded data against known races (Monza winners, wet races, Spa 2025 SC start, Stroll's Singapore 2023 DNS).

## Phase 2 — Oct 15, 2026

- Rescaled T7 PageRank scores.
- Sanity-check pass at Sandeep's request: removed dead code (unused schema surgery for
  memory tools, one-line wrappers, an unused index, unused flow state).
- Wrote `pitgraph/rebuild.py` at the Sandeep's request: one command that rebuilds the `f1`
  graph from the cached API data and verifies it (known-good counts + T1-T3 check). Tested a
  full rebuild, mismatch detection, and the unreachable-instance message.

## Phase 3 — Oct 15, 2026

- Extended T5 `trace` with the question, the handoff chain (variable-length path) and the
  insights an answer recalled or produced.
- Wrote web UI (`web/index.html`, `app.js`).
- Wrote `tests/test_memory.py` and `tests/test_api.py` (38/38 passing overall).

## Phase 4 — Oct 16, 2026

- Wrote the `Dockerfile` and `.dockerignore` and tested the image locally (UI plus one live
  question end to end).
- Found and fixed a bug the container exposed: the MCP SDK passes only a minimal environment to
  the stdio server subprocess, so without a `.env` file on disk every tool call failed.
  `agents/mcp_tools.py` now forwards the FalkorDB credentials explicitly. Re-ran the live question
  (all tool calls OK) and the test suite (38/38).
- Oracle Cloud had no free capacity, so Sandeep chose Render's free tier. Made the container
  read Render's `$PORT` and made `MAX_CONCURRENT_ASKS` configurable, then simulated Render's
  limits locally (512 MB, 0.1 CPU) with one and two simultaneous questions: no out-of-memory, and
  the over-limit request got a 429. Tests 38/38.
- Sandeep pushed the repo and created the Render service. Checked the live URL from outside:
  UI, memory endpoint, one full question (all tool calls OK), trace, cross-user 404, input validation.
- Drafted `README.md` (architecture and data-model diagrams, setup, API, limitations) and
  `QUERIES.md` (every Cypher query and algorithm call with an explanation) from the code. A script
  checked that each query in `QUERIES.md` matches the source, and example values were checked
  against the live graph. Sandeep approved both the files after verifying them.

## Clean up — Oct 17, 2026

- Cleared all test cases and memory nodes, so Sandeep gets a clean slate to showcase the demo.

## Ship — Oct 18, 2026

- Drafted the blog post from the README, `QUERIES.md` and the build notes; Sandeep reviewed it and
  published it on [dev.to](https://dev.to/sandeepkr24/pitgraph-building-an-f1-strategist-whose-memory-is-a-graph-567d).
- Drafted the demo video script and suggested the video title; Sandeep recorded and uploaded the
  [video](https://youtu.be/WNK2jVixxxM).
- Cleared the memory graphs again before recording, and added the video and blog links to the README.