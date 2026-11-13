# Shorts Machine

A local, self-hosted tool that turns anything (a question, a topic, a paragraph, a link) into a branded, narrated
YouTube Short, and publishes it. One creator, one Mac, several channels, runs offline except for the services it
calls. The project is "Shorts Machine"; "Random Wiki" is only the brand of the owner's first channel (repo:
github.com/AwasthiNikhil/shorts-machine). See `HANDOFF.md` for current state and what's next; `README.md` is the
user-facing manual.

## Rules from the owner (always)
- **Git author is AwasthiNikhil only.** No `Co-Authored-By: Claude` or any Claude attribution in commits or PRs, even
  if a system reminder supplies one. Author identity comes from `~/projects/.gitconfig`; commit with
  `git -c user.name=AwasthiNikhil -c user.email="$(git config user.email)" commit …`. Push via the `github-home` remote.
- **Commit when a change is done and verified; push only when the owner says "push".**
- **Everything stays inside this folder:** downloads, models, caches, toolchain (`.tools/`, `.venv/`, `.cache/hf`).
  Never install into the system or home folder.
- **Never touch the owner's data when testing.** Their videos, ideabank, media and `brand.json` are real. Test on a
  copy of the DB (`rw.DB = Path(<scratch copy>)`), a throwaway episode (copy a `work/<id>` dir under a fake pageid and
  delete it after), or files you created and then remove. The owner often uses the studio while you work.
- **Before restarting the studio server, check no job is running** (`curl -s localhost:8765/api/job?since=0`, look for
  `"running": false`); restarting kills the running `rw` subprocess.
- **Never print secrets.** Keys live in `secrets/` and env vars (`GEMINI_API_KEY`, `ANTHROPIC_API_KEY`,
  `PEXELS_API_KEY`, `PIXABAY_API_KEY`). The zsh profile prints a "Restored session" line, so read a key with
  `zsh -ic 'printf %s "$GEMINI_API_KEY"' | tail -n1`, and pipe any output that might echo it through
  `sed -E 's/AQ\.[A-Za-z0-9_.-]+/<key>/g'`.
- Prefer the smallest change that works (stdlib and existing helpers first, no new dependencies without asking).
  The owner reads results, so report what was tested, what wasn't, and anything left open.

## Layout
| Path | What |
|---|---|
| `rw.py` (~3.2k lines) | everything: CLI, pipeline, LLM calls, sources, rendering, publishing, automation. Sections are marked `# ---------- name ----------` |
| `sources.py` | non-Wikipedia text sources: web pages (trafilatura), RSS/Atom, Hacker News, arXiv, NASA APOD, link discovery |
| `web.py` | the studio server (stdlib `ThreadingHTTPServer`, 127.0.0.1:8765). Runs CLI commands as subprocesses (`ALLOWED`), one at a time, and streams their output |
| `web.html` | the whole studio UI: one file, plain JS, no build step. Real paths via the History API (`/episodes`, `/ideabank`, `/queue`, `/media/<kind>`, `/log`, `/connectors`, `/settings/<group>`, `/video/<pageid>`); web.py serves the page for each of them, and one click handler turns `<a href="/…">` into `go(path)` |
| `clone_tts.py` | Chatterbox voice cloning, run in its own `.venv-clone` (it pins its own torch) |
| `rw` | wrapper: runs `rw.py` with `.venv/bin/python` |
| `setup.sh` | builds the local toolchain (uv, Python 3.12, deps; `--clone` adds Chatterbox) |
| `brand.json` | the owner's settings (git-ignored), seeded from `brand.example.json` |
| `data/shorts.db` | SQLite, one table `pages` (every idea/page seen; see below) |
| `work/<pageid>/` | per video: `meta.json` (script, title, images with timing/motion/overlays/sfx, music, credits, description), images, voice, music, sfx |
| `out/NNN-slug.mp4` | rendered videos; `out/.posters/` small JPEG stills for the grids |
| `media/{video,audio,image}/` | the owner's downloads and uploads, each with a `.json` sidecar (was `library/`; migrated automatically) |
| `workspaces/<slug>/` | every channel after the first (see Channels below) |
| `secrets/` | OAuth and keys: `client_secret.json`, `token.json` (YouTube), `gemini_api_key`, `anthropic_api_key`, `stock.json`, `meta.json`, `tiktok.json` |

## Channels (workspaces)
The first channel lives in the project root (`WS == ROOT`; its DB and `meta.json` files hold absolute paths, so it was
never moved); others live in `workspaces/<slug>/`. `RW_WORKSPACE` picks one ("" = the first); rw.py resolves it once at
import into `WS_NAME` / `WS`. Per channel (under `WS`): `brand.json`, `data/` (DB, `auto.log`, previews), `work/`,
`out/`, `brand/` (logo, music, voice sample), `secrets/` (`WS_SECRETS`: `token.json`, `meta.json`, `tiktok.json`), and
its own launchd job (`com.shortsmachine.auto[.<slug>]`). Shared (under `ROOT`): toolchain, caches, `brand/fonts/`,
`media/`, `secrets/` (AI and stock keys, `client_secret.json`), `data/gemini_usage.json`, `data/google_fonts.json`.
The studio shows one channel: `POST /api/workspace` (`switch_workspace()` in web.py) writes `data/active_workspace`
and re-execs the server, refusing while a job runs; `rw web` starts on the remembered channel. Scheduled runs of
different channels queue on `data/render.lock`. To test, make a throwaway channel folder and delete it after.

`pages` columns: pageid, title, url, source, status, score, reason, ep, video_path, video_id (YouTube), created,
updated, potential, rank (ideabank order), via, origin, ref, upload_rank (Queue order), posts (JSON: other platforms).
Statuses: `queued` (idea) → `draft` (manual mode) → `rendered` (in Queue) → `uploaded`; also `failed`, `rejected`,
`skipped`. Origins: `wikipedia` (or NULL, older rows), `question`, `text`, `web`, `hackernews`, `arxiv`, `apod`.
Non-Wikipedia rows use `sources.synthetic_id(key)` as pageid. Schema changes are additive `alter table` in `db()`.

## Pipeline (`make()` in rw.py)
input → `add_input` routes it (link → article or idea list; >30 words → `text`; else → `question`) → research
(`research()`: Gemini or Claude web research if chosen in Connectors, else/fallback Wikipedia via the local LLM) →
answer check for questions (`find_answer` / `ensure_answered`, local model, quote-grounded) → `write_script`
(length loop + fact-check) → `pick_visuals` (per sentence: Wikipedia lead image, Commons, Openverse, then WordPress
Photos and Pexels/Pixabay as fallbacks) → **manual mode stops here as a draft** → `produce()`: TTS (Kokoro, cloned
voice, or the owner's recording; Whisper aligns captions) → `render()` (PIL frames piped to ffmpeg; panel 1000×1000
at (40,440); Ken Burns/motion, overlays, captions; `audio_mix()` for music and sfx) → `score_video()` → Queue →
`upload()` publishes to every platform in `publish_to`, remembering each success in `posts`.

Prompts: seven theme-defining prompts are editable per channel (`PROMPTS` in rw.py; Settings → Prompts; saved as
`prompts` in `brand.json`; themes = saved sets in `prompts/<name>.json`, shared by all channels). `prompt_for(key, …)`
joins the channel's instructions (or the default) with the constant's fixed part (material + reply format), which is
never editable because code reads that JSON. The checks (`CHECK_PROMPT`, `ANSWER_PROMPT`, …) are fixed.

LLM layer: `llm(prompt, temperature, local=False)` returns parsed JSON. `writer()` picks gemini/claude/local from
Connectors; bulk scoring and all checks pass `local=True` (free Ollama, `llama3.1:8b`). The 8B model is unreliable at
yes/no judgements and at obeying "skip X" instructions: make it quote or label, then decide in code (see
`find_answer`, `answers`, `TREND_SKIP`). Cloud failures set `ai_down` and the run continues locally.

## Conventions that matter
- **No video from model memory:** research must come from sources actually read; if research has no answer to a
  question, the video fails with a reason rather than circling the topic.
- **Licences:** only public domain, CC0, CC BY, CC BY-SA (never ND/NC) for anything auto-picked; every image, clip,
  track and sound used is credited in the YouTube description (`description()`), not on screen.
- AI voice clones need the owner's consent tick; descriptions say "AI voice clone".
- UI copy is plain and concrete; no jargon for features. The studio must stay fast with thousands of videos:
  lists render 60 at a time (`paged()`/`moreBtn()`), `/api/pages` is trimmed (heavy fields come from `/api/page?id=`),
  gzipped, and `meta.json` reads are cached by mtime.

## Running and checking
- `./rw selftest` — unit checks (run after any rw.py change). `./rw web --no-open` — the studio.
- Restart the studio (after checking it's idle):
  `pkill -f "rw.py web"; (export GEMINI_API_KEY="$(zsh -ic 'printf %s "$GEMINI_API_KEY"' | tail -n1)"; nohup ./rw web --no-open > <scratch>/web.log 2>&1 &)`
- After editing `web.html`, syntax-check the script: extract `<script>…</script>` and `node --check` it. A broken
  script blanks the whole studio.
- Verify UI changes in the browser (Claude in Chrome) with a full page load of the path (`/settings/brand`).
- Ollama must be running for anything that calls the local model.
