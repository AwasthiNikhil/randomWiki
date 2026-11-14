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
| `web.html` | the whole studio UI: one file, plain JS, no build step. Real paths via the History API (`/episodes`, `/ideabank`, `/queue`, `/media/<kind>`, `/log`, `/connectors/<group>`, `/settings/<group>`, `/video/<pageid>`); web.py serves the page for each of them, and one click handler turns `<a href="/…">` into `go(path)` |
| `clone_tts.py` | Chatterbox voice cloning, run in its own `.venv-clone` (it pins its own torch) |
| `rw` | wrapper: runs `rw.py` with `.venv/bin/python` |
| `setup.sh` | builds the local toolchain (uv, Python 3.12, deps; `--clone` adds Chatterbox) |
| `brand.json` | the owner's settings (git-ignored), seeded from `brand.example.json` |
| `data/shorts.db` | SQLite, one table `pages` (every idea/page seen; see below) |
| `work/<pageid>/` | per video: `meta.json` (script, title, images with timing/motion/overlays/sfx, music, credits, description), images, voice, music, sfx |
| `out/NNN-slug.mp4` | rendered videos; `out/.posters/` small JPEG stills for the grids |
| `media/{video,audio,image}/` | downloads and uploads, shared by all channels, each with a `.json` sidecar: source, licence, `by: "clips"` when fetched automatically for a clip video, and `used_in` (which channel's video uses it, as `used` or `spare`; kept current by `media_used()` / `clips_usage()` / `media_forget()`). The Media tab filters on these |
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
A video can be copied to another channel (`./rw copyto --ep N --to <slug>`, the "Copy to channel" button): `copy_to()`
starts `copyfrom` as the target channel, where `copy_from()` copies the script and pictures (or clip list) into its
own `work/`, gives it the next episode number and makes it with that channel's brand and voice.

`pages` columns: pageid, title, url, source, status, score, reason, ep, video_path, video_id (YouTube), created,
updated, potential, rank (ideabank order), via, origin, ref, upload_rank (Queue order), posts (JSON: other platforms).
Statuses: `queued` (idea) → `draft` (manual mode) → `rendered` (in Queue) → `uploaded`; also `failed`, `rejected`,
`skipped`. Origins: `wikipedia` (or NULL, older rows), `question`, `text`, `web`, `hackernews`, `arxiv`, `apod`.
Non-Wikipedia rows use `sources.synthetic_id(key)` as pageid. Schema changes are additive `alter table` in `db()`.

## Pipeline (`make()` in rw.py)
input → `add_input` routes it (link → article or idea list; >30 words → `text`; else → `question`) → research
(`research()`: Gemini or Claude web research if chosen in Connectors; else/fallback `research_web()`, which searches
without a key (`sources.search_web()`: DuckDuckGo's plain page, Bing RSS as backup), reads up to four pages from
different sites and uses their text as notes; Wikipedia via the local LLM is the last resort, or the only step when the
channel's `research_from` is "wikipedia") →
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

Models the owner can add (Connectors → AI model): other writers through `PROVIDERS` (Groq, OpenRouter, Cerebras:
OpenAI-style chat, keys in `secrets/ai_keys.json`, `provider_chat()`; they write, research stays `research_web()`);
Ollama models downloaded with `./rw pull <name>`; a picture model on this Mac (`./rw imagemodel`: mflux in
`.venv-image`, FLUX.1 schnell 4-bit in `.cache/hf`, `generate_image()`, offered in the visuals editor as source `ai`
and saved to `media/image/` as "AI-generated"; a channel with `pictures: "ai"` has every picture drawn instead of
picked: `draw_visuals()` asks for one scene per sentence (`DRAW_PROMPT`) and appends the channel's `picture_style`); voices: Kokoro, the clone, or Edge online voices (`tts_edge()`, word
timings from the service, any language; `edge_voices()` cached in `data/`). Picture sources with the owner's own free
keys live in `STOCK` (Pexels, Pixabay, Unsplash, Smithsonian, Europeana); keyless ones are plain `src_*` functions.

LLM layer: `llm(prompt, temperature, local=False)` returns parsed JSON. `writer()` picks gemini/claude/a provider/local from
Connectors; bulk scoring and all checks pass `local=True` (free Ollama, `llama3.1:8b`). The 8B model is unreliable at
yes/no judgements and at obeying "skip X" instructions: make it quote or label, then decide in code (see
`find_answer`, `answers`, `TREND_SKIP`). Cloud failures set `ai_down` and the run continues locally.

## Clip countdowns (`make_clips()` in rw.py)
A second kind of video, chosen per channel (`video_type: "clips"` in brand.json; Settings → Ideas & videos) or run
directly with `./rw clips "<words and/or links>" --count N`. No research, script or voice: `clip_candidates()` reads
YouTube Shorts under the words as a hashtag (kept only if the title names the subject, `CLIP_FILLER` words aside),
plus any links and the channel's `clip_sources` (yt-dlp: single video, profile or playlist; TikTok accounts work,
TikTok tags/search, Facebook and Instagram listings and Reddit's API don't). The most viewed are taken whole, never
cut: only clips of `CLIP_SECONDS` (4-30 s) that fit `max_seconds` together, and not ones with text on screen
(`has_text()`: 3 frames through `ocr.swift`, a macOS Vision helper compiled once into `.tools/ocr`; pasted single
videos skip this check). They are downloaded into `media/video/`, each is fitted to 1080×1920 over a blurred copy
with a rank badge, and the pieces are joined with their own sound. With `clip_strip_voice` on, `strip_voice()`
(Demucs `htdemucs`, CPU, model in `.cache/torch`) first removes human voices, speech and singing, from each clip. Used clip links are remembered in the channel's
`data/clips_used.json`. On a clips channel, `run` and `auto` call it (no input = a random channel topic).
Three steps: `make_clips()` gathers (the ones to use plus `CLIP_SPARES`, each with `use`; manual mode stops here as a
draft), `clip_add()` (`./rw clipadd --ep N <link>`) appends one video, `render_clips()` joins the clips marked `use` in
their saved order (last = number 1) and is what `rerender` runs for these videos. Rendered pieces are kept per clip
and rank in `work/<id>/seg-*.mp4`, so a reorder is quick. The studio's clip editor (`clipEditor`/`wireClips` in
web.html) saves order and ticks through `POST /api/clips`.
**The owner chose to allow other people's clips here**, as an exception to the licence rule below; every clip is
credited in the description, and publishing rights are theirs to sort out.

## Quizzes (`make_quiz()` in rw.py)
A third kind of video (`video_type: "quiz"`; `run` and `auto` on such a channel take a topic, or a random channel
topic). `research()` as for explainers, then `quiz_questions()`: the writer's model drafts questions with three options
and the sentence of the notes that proves each; code keeps only those whose sentence is really in the notes
(`in_text()`), names the answer and neither wrong option, and from which the local model, shown only that sentence,
picks the same option. Fewer than three kept = the video fails. `produce_quiz()` voices line by line (intro, each
question, each answer, outro), adds `QUIZ_THINK` seconds with a tick per second and a chime on the answer, drops the
last question while the total is over `max_seconds`, and `render_quiz()` draws it (cards cached per state; `backdrop()`,
`font()`, `fit_lines()`, `encode()` are shared with `render()`). Meta has `origin: "quiz"`, `intro`, `quiz`
(question, options, answer, quote). `rerender` and "Copy to channel" re-voice and redraw it. No draft step, no
question editing, no pictures or captions; the studio lists the questions with their proof. In the UI a quiz channel
counts as an explainer channel for `data-for` (same topics, research, voice and model settings).

## Conventions that matter
- **No video from model memory:** research must come from sources actually read; if research has no answer to a
  question, the video fails with a reason rather than circling the topic.
- **Licences:** only public domain, CC0, CC BY, CC BY-SA (never ND/NC) for anything auto-picked; every image, clip,
  track and sound used is credited in the YouTube description (`description()`), not on screen.
- AI voice clones need the owner's consent tick; descriptions say "AI voice clone".
- Settings and Connectors share one layout (left nav, page title with a scope pill saying "Only for <channel>" or
  "All channels"). Anything that applies to one kind of channel carries `data-for="explainer"` or `data-for="clips"`
  and is hidden by CSS from `body[data-vtype]` (`setVtype()`); add that attribute to new type-specific controls.
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
