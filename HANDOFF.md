# Handoff: state as of 2026-11-13, and what's next

Read `CLAUDE.md` first (layout, rules, pipeline). This file is the snapshot of where things stand and the
project the owner wants next. Everything below is committed on `main`; the last push was `aa60906`.

## What exists today
- **One box to make a video** from a question, topic, paragraph or link; **auto or manual mode** (manual stops at a
  draft: edit script, per-visual timing, motion, callouts (arrows/circles/labels), sound effects, per-video music;
  then render).
- **Ideas:** ideabank with drag order; when empty, a mix of fresh angles on the channel's topics and questions behind
  this week's news in them (Google News + Google Trends RSS, filtered in code by category and fit). Random Wikipedia
  is still an option, no longer the default.
- **Research:** Gemini (Google Search, or reads pages it picks) or Claude (web search), else Wikipedia via the local
  model. Two-step answer check for questions.
- **Voices:** Kokoro presets, a cloned voice (Chatterbox, consent required), or the owner's own recording.
- **Media tab:** download video/audio (yt-dlp) and images (direct link, Commons page, a page's og:image), or upload
  from device; use any of it in a video. Free music and sound effects search via Openverse.
- **Connectors:** AI model choice (local / Gemini / Claude); stock keys (Pexels, Pixabay); publishing to YouTube,
  Facebook Page, Instagram (Reels via Meta Graph API), TikTok (inbox upload).
- **Automation:** macOS launchd job (Settings → Automation) makes and optionally publishes on a schedule.
- **Channels:** several channels, one open at a time, each with its own brand, ideas, videos, schedule and sign-ins.
- **Studio:** a real address for every view, Settings split into Brand / Voice & music / Ideas & videos / Automation, paging
  and posters for thousands of items.

## Untested or open
- **Never tried with real accounts or keys:** Claude (only the bad-key path), Facebook, Instagram, TikTok publishing,
  Pexels and Pixabay (the owner has since saved a stock key in `secrets/stock.json`, so one of those can be tried).
  The code follows current docs (Graph API v27.0; TikTok Content Posting API, desktop OAuth with hex PKCE on
  `http://127.0.0.1:8766/callback/`).
- **Images from galleries/social posts** (Instagram carousels, Reddit galleries, Flickr albums) aren't supported;
  suggested adding `gallery-dl`, owner hasn't decided.
- **Research no longer defaults to Wikipedia:** `research_web()` (keyless search, reads four sites) runs before it;
  tested on three questions and one full draft ("why is the sky blue": NASA, Mental Floss, Britannica, Live Science).
  The answer check now accepts near-verbatim quotes (a word dropped or added), which web text needed. Risks: the
  search reads DuckDuckGo's page for people and may get refused; any site in the results is trusted equally.
- **Still Wikipedia-coupled:** topic brainstorms require and verify a Wikipedia article per idea; "Random Wikipedia
  pages" as an idea source; auto-picked visuals try Wikipedia lead images first; non-Wikipedia ideas get neutral
  demand numbers in scoring. The local judge in the answer check is still inconsistent (same quote judged both ways).
- Gemini on the owner's key: Google Search grounding returns quota 429 (falls back to url_context); models are often
  "high demand" (503), so runs regularly fall back to the local model.
- The owner rotated (or was told to rotate) their Gemini key after it was once printed in a session.

## Done: channels (workspaces) and real routes
Several channels from one studio; see "Channels" in `CLAUDE.md` for how it works. Routes are real paths now
(`/episodes`, not `#/episodes`). `data/randomwiki.db` was renamed `data/shorts.db`.

Tested: routes (click, back, reload, old `#/` bookmarks, video dialog), creating a channel and switching both ways in
the browser, isolation of the new channel's folder, `RW_WORKSPACE` on the CLI, selftest.
Not tested: a second channel's YouTube sign-in, making a video in a second channel, per-channel launchd jobs and the
shared `render.lock` (only the job label and paths were checked), refusing to switch while a job runs.

Also done: Settings → Prompts (per-channel prompt editing with shared themes; tested in the browser on a throwaway
channel: edit, reset, save theme, load theme, save; not tested: making a video with an edited prompt).
Limits the owner knows: scripts are still forced to 85-128 words and fact-checked whatever the prompt says;
`TREND_SKIP` still drops sports/celebrity/politics headlines in code; a prompt can't change the kind of video
(narrated explainer). The owner's other theme ideas (compilations of funny clips, gameplay clips) need a new video
type, not a prompt: find clips → rank → cut and join, no narration; and reposting other people's clips conflicts
with the licence rule in CLAUDE.md. Not built; owner to decide.

Also done: clip countdowns (see CLAUDE.md). Tested: a 3-clip video from the words "funny cats" in a throwaway
channel (45 s, 1080×1920, sound, badges, credits), shown and playable in the studio; the channel setting saves.
Not tested: links from TikTok / Instagram / Facebook / Reddit, a profile or playlist link, a clip with no sound,
scheduled runs on a clips channel, uploading one.
Since then: clips play whole (4-30 s), text-on-screen clips are dropped (ocr.swift), hashtag results must name the
subject in the title, and channels have a `clip_sources` list (a TikTok account was listed and downloaded in a test;
its clips were then dropped for on-screen text). Voice removal (Demucs, `clip_strip_voice`) was tested with a known sentence mixed over a clip's music: recognised
before, only music after; about a quarter of a clip's length to process. Nobody has listened to the result.
Clip editor (pick, reorder, add by link, re-render) and the Settings/Connectors redesign were tested in the browser
on Rank Them and a throwaway channel; drag-and-drop reordering was not exercised (the up/down buttons were), nor was
the redesign checked at phone width or on Random Wiki itself.
Media tracking (usage labels, by-you/fetched filter, cleanup button) was checked in the browser on Rank Them; the
cleanup button and deleting a used item were not clicked through, since they would delete the owner's real clips.
Added 2026-11-14 (see CLAUDE.md "Models the owner can add"): Unsplash/Smithsonian/Europeana (tested with the two
public demo keys and a bad Unsplash key; no real Unsplash key tried), Prelinger films (one clip fetched), Groq/
OpenRouter/Cerebras writers (only the bad-key path; default model names may be out of date, the list comes live once a
key is saved), `pull` (tested with a tiny model, removed after), Edge voices (English and Nepali sentence synthesised
with timings; no full video rendered with one), the picture model (see below for whether its first run finished).
Drawn-picture channels (`pictures: "ai"`) were proven with one full video on a new channel, Odd Origins (EP 001,
barber poles, 40 s, six drawings, Edge voice). Known weak spot: drawings illustrate the sentence, not verified facts,
so they can show things wrongly (era, details).
Copy to another channel: tested from the command line (a clip video and a narrated video into a throwaway channel,
and a refused duplicate); in the studio only that the button and channel list appear, not a click.
Quizzes (2026-11-14, see CLAUDE.md "Quizzes"): a new channel, 3 Second Quiz (`workspaces/3-second-quiz`), with one
video made from "the solar system" (5 questions, 54 s, Gemini research, Kokoro voice); re-render run once; the studio's
tile, Make-box hint and question list were looked at in the browser. Not tested: Make now from the studio, a scheduled
run, copying a quiz to another channel, Edge or cloned voices, the local model writing the questions, and nobody has
listened to the ticks and chime. Open: random topics repeat, so questions can repeat across videos (nothing remembers
asked questions); questions can't be edited in the studio; the Videos settings page still shows idea, picture and
edit-first options that a quiz channel ignores.
Later the same day: quiz questions are editable in the studio (tested in the browser on a throwaway copy: changed a
question, changed which option is right, removed one, added one, re-rendered, frames checked). Two more formats and
channels (CLAUDE.md "Formats"): Sharp Eye (`workspaces/sharp-eye`, guess the picture; EP 001 famous landmarks from the
command line, EP 002 dog breeds from the studio's Make now) and Today in History (`workspaces/today-in-history`, on this
day; EP 001 for 14 November). Not tested for these: scheduled runs, copying to another channel, Remake, a typed date,
Edge or cloned voices; nobody has listened to any of them. Weak spots: a guess picture can give itself away (a sign, a
caption in the photo) and Wikipedia's main picture isn't always the best one; one-line descriptions can be bland
("Dog breed"); the history word list drops harmless events and can let a grim one through; history pictures are of a
linked article, which may be a person or place rather than the event; Wikipedia rate-limits picture lookups, so runs
sometimes wait 10-40 s. Guess and history videos can't be edited in the studio (only remade).
Known gaps: topic match is by title words only; small or brief
stamps and emoji overlays pass the text check; letting Gemini watch and rate candidates is the real fix. The video's dialog still shows the explainer editor (narration,
visuals), which doesn't apply; re-render is refused. No per-clip trimming or reordering yet; each clip is its first
N seconds. Only YouTube is searched by words; other sites have no usable search in yt-dlp, so they need links.

Open:
- The project folder is still `~/projects/randomwiki`. Renaming it breaks the absolute paths in the first channel's DB
  (`video_path`) and `work/*/meta.json` (image paths), `.venv` scripts, and the launchd job; it needs a path rewrite
  and `./setup.sh` again. Owner to decide.
- No "delete channel" in the studio (delete `workspaces/<slug>/` by hand, after turning its schedule off).
- AI model choice (`ai`, `llm`, `gemini_model`) sits in `brand.json`, so it is per channel.
- Replacing the shared YouTube client file signs out only the open channel; other channels' tokens go stale and need
  Connect again.

## Next project: make it scalable (the owner suggested Next.js)
Be clear about what's being scaled before choosing tech: one creator with many channels on one Mac, or a hosted
product for many users? The answer changes everything below, so ask the owner.

What Next.js would and wouldn't replace:
- It's a good fit for the **studio UI** (`web.html` is one 1.7k-line file of plain JS) and an API layer.
- The **heavy work must stay Python**: Kokoro/Chatterbox TTS, faster-whisper alignment, PIL+ffmpeg rendering,
  yt-dlp, trafilatura, the LLM orchestration. A sensible shape is Next.js (UI) → a Python API (e.g. FastAPI) → a job
  queue → Python workers. Today `web.py` runs one CLI subprocess at a time; that single-job runner and SQLite are the
  real limits, more than the UI.
- For a hosted product on top: Postgres instead of SQLite, object storage for media/videos, auth and per-user
  workspaces, render workers (CPU/GPU), and per-user OAuth with Google's app verification and the YouTube API audit
  (unaudited apps keep uploads private and are capped at 100 users), plus Meta App Review and TikTok's audit.
- Suggested order (workspaces are done): split `rw.py` into modules behind a small Python API with a jobs table/queue, then build the new UI against
  that API, keeping feature parity with the current studio.

## Useful context
- The owner works in English but some sites they test are Nepali (e.g. sujitapandey.com.np).
- The full transcript of the long session that built most of this is in
  `~/.claude/projects/-Users-nikhilcodewing-projects-randomwiki/86a5ac6d-608e-4d97-93a0-5e3c6f19e265.jsonl` if a
  detail is missing here (large; search it rather than reading it whole).
