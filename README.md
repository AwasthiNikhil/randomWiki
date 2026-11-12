# Shorts Machine

Type anything: a question, a topic, a paragraph or a link. A branded YouTube Short comes out. Everything runs locally and costs nothing.

## One box
In the studio's **Make a video** box, or `./rw run "…"` (`./rw add "…"` saves it as an idea instead):
| You type | What happens |
|---|---|
| a question or topic: *How does a search engine work?*, *black holes* | the local LLM picks the Wikipedia articles that cover it (plus the Simple English version of the main one), and the script answers it in steps ("First… Next… Finally…"), using only those facts |
| a paragraph (over 30 words): notes, a story, a draft | your text becomes the script, keeping your facts and order. Short texts get background notes from Wikipedia. Your facts aren't fact-checked against Wikipedia, so it's on you to get them right |
| a link to an article: Wikipedia, any web page, Hacker News, arXiv, NASA APOD | that page is made into a video |
| a link to a list: a feed, a site, a subreddit, a Wikipedia category or list | its most interesting items go into the ideabank |
| nothing | the next idea in the ideabank, else a random interesting Wikipedia page |

## Gemini (optional)
Everything runs locally by default. With a Gemini API key (free at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)), Gemini does the thinking instead:
- **Research goes beyond Wikipedia.** Gemini uses Google Search if your key's plan includes it. Otherwise it picks reliable pages on any site (encyclopedias, official docs, universities), reads them itself, and writes notes from what it read. Every page it read is credited. If it can't read enough, research falls back to Wikipedia rather than script from the model's memory.
- **Visuals:** Gemini names the concrete things to show, and their Wikipedia articles supply the images.
- **Writing:** scripts, fact-checks and image picks use Gemini. Scoring ideas in bulk stays on the local LLM to save quota. If Gemini fails (quota, busy), the local LLM takes over for the rest of that run.

The key is yours, not the project's: `GEMINI_API_KEY` in your shell is used first. You can also paste one in **Settings → Content**, which saves it to `secrets/gemini_api_key` (git-ignored). Scheduled runs need the saved key, since they don't load your shell profile. The model is chosen there too; `gemini-flash-latest` follows Google's current Flash model.

**Usage:** each video shows the Gemini requests and tokens it used (Episodes → open a video), and Settings shows today's totals per model. Days follow Pacific time, when Google resets the free quota. The API doesn't report how much quota is left; see [ai.dev/rate-limit](https://ai.dev/rate-limit).


1. **Pick:** random articles, or ideas found around a topic through its Wikipedia link network (1–3 hops). Stubs, lists and disambiguation pages are dropped, and a local LLM scores the rest out of 10.
2. **Log:** every page that comes up is stored in SQLite with a status and the reason, so nothing is ever processed twice.
3. **Script:** Ollama writes a 45–55s narration from the full article, then fact-checks it against the article. You can edit it before rendering again.
4. **Visuals:** chosen from what the narration mentions, sentence by sentence. Each named thing (a person, place, organisation, product or logo) gets the lead image of its own Wikipedia article. Named things without one are searched in Commons and Openverse, needing all the words to match plus an LLM meaning check. Vague phrases keep the previous image rather than show a wrong one. Each image appears when its sentence is spoken; logos and screenshots are shown whole on a light card. Falls back to the article's own images. In the visuals editor you can **Auto-pick from script**, search NASA and The Met, or upload your own.
5. **Voice:** one of three:
   - a **Kokoro preset voice**: fast, the default
   - **your cloned voice**: Settings → Voice → *My voice*. Record a 15–30 s sample in the studio; it's cloned locally with Chatterbox (MIT licence), and the output carries an inaudible AI watermark. Install it with `./setup.sh --clone`.
   - **your own recording**: open a video → *Record it in your voice*. A teleprompter shows the script; record with your mic, and the video re-renders with your voice.

   Captions are timed to whatever was spoken (local faster-whisper alignment) and show the text as written. Numbers, versions and Roman numerals are spoken correctly ("5.4–8.5" → "5 point 4 to 8 point 5", "World War II" → "World War Two").
6. **Render:** a branded 1080×1920 layout drawn with Pillow and encoded with ffmpeg.
7. **Score:** each video gets a 0–100 potential score from topic demand, hook, appeal, reach, visuals and length.
8. **Publish:** YouTube Data API upload, private or scheduled, best potential first. Credits go into the description automatically.

## Video clips
Visuals can also be **video clips**: free-licensed Wikimedia Commons clips (using Wikimedia's own small transcodes) and NASA footage (public domain). They're trimmed to 12 s, skip any opening slate, are muted, and play in the panel. Auto-pick can choose them for named things; in the visuals editor, pick *▶ Commons videos* or *▶ NASA videos*.

## Sources
| Source | Find ideas | What the script is written from | Images |
|---|---|---|---|
| Wikipedia | random, or a topic's link network (1–3 hops) | the article | the article's own, then Commons |
| Any web page | paste a link | the page's main text (extracted with trafilatura; robots.txt respected) | Commons/Openverse search, never the page's own images |
| RSS / Atom | the latest items of a feed URL | each item's linked page | as web |
| Hacker News | today's top stories | the linked article, or the post text | as web |
| arXiv | papers matching a topic | the abstract (arXiv metadata is CC0) | Commons search, else a title card |
| NASA APOD | the latest pictures (via NASA's RSS feed) | the explanation | search; APOD photos are often copyrighted, so they're not reused automatically |

**Find ideas** in the Ideabank takes a topic (explored through Wikipedia's links), a link, or nothing (random). A link (`./rw discover --topic URL`) is worked out what you pasted: an RSS/Atom feed, a site or blog (its feed is found automatically), a subreddit (link posts are read from the article they point to), a Wikipedia `Category:` or `List of…` page (its articles), a Wikipedia article (explores around it), a Hacker News list, an arXiv listing, a single article, or any page that lists articles (news sections, blog indexes). Automation can use a link as its source when the ideabank is empty.

Credits name the source and link back. Non-Wikipedia text is copyrighted by its authors: the narration is a short summary in new words, so review it before publishing.

```
./rw run https://news.ycombinator.com/item?id=8863     # any supported link works
./rw discover --topic https://www.nasa.gov/feed/      # ideas from a feed, site or list
```

## Requirements
- macOS or Linux, about 2 GB of disk, `curl` and `bash`
- [Ollama](https://ollama.com) running locally, with the model named in `brand.json` pulled:
  ```
  ollama pull llama3.1:8b
  ```

Everything else (uv, Python 3.12, Python packages, the bundled ffmpeg, fonts, the voice model) is installed **inside this folder** by `setup.sh`. Nothing is installed system-wide.

## Setup
```
git clone git@github.com:AwasthiNikhil/shorts-machine.git
cd shorts-machine
./setup.sh          # about 2 GB download, ends with "selftest ok" (add --clone for voice cloning, ~1.5 GB more)
./rw web            # opens the studio at http://127.0.0.1:8765
```
The voice model (about 300 MB) downloads on the first render.

## Studio (`./rw web`)
A local web UI. It's bound to 127.0.0.1 and runs the same commands as the CLI, streaming their output into a terminal panel.
- **Episodes:** previews you can search, filter and sort by potential. Open one to watch it, edit its title, narration and visuals, then **Save & re-render**. You can also upload it, skip it, delete it, or delete it from YouTube.
- **Queue:** videos ready to upload, in the order they'll go out. Drag to reorder; videos you haven't placed are sorted by potential score. **Upload pending** and scheduled runs take from the top.
- **Ideabank:** what gets made next. Drag to reorder, paste links in bulk, or **Find ideas** randomly or around a topic, with a degree-of-relation control.
- **Page log:** every page seen, with reasons. Sortable and searchable, with multi-select delete. Uploaded pages are always kept.
- **Settings:** channel, colours, Google Fonts, voice, music, LLM and thresholds, with a real-frame preview.

## CLI
```
./rw run ["anything"]         # a question, topic, paragraph or link; nothing = the next idea, else a random page
./rw add "anything" [...]     # save ideas for later runs
./rw discover [--count 5] [--topic nepal|URL --degree 1|2|3|any]   # find ideas: random, a topic's link network, or a link
./rw run --upload [--publish-at 2026-11-12T15:00:00Z]
./rw upload [--ep N]          # upload every rendered video not yet uploaded (best first), or just episode N
./rw unpublish --ep N [--keep-local]   # delete an uploaded episode from YouTube (and locally unless --keep-local)
./rw rerender --ep N          # re-voice and re-render from the saved (hand-edited) script and visuals
./rw score [--all]            # predict how well each video may do (0-100)
./rw preview                  # render one frame with the saved brand settings -> data/preview.png
./rw voicetest [--text ...]   # speak a sample with the saved voice -> data/voice_preview.wav
./rw log [N]                  # rejected / queued / rendered / uploaded / failed, with reasons
./rw auto [--dry-run]         # one scheduled run: make the next video (discover one if the queue is empty), upload
./rw schedule                 # install/remove the launchd job from brand.json's schedule
./rw web [--port 8765]        # the studio
./rw selftest
```
`--force` re-makes a page that was already rendered or uploaded. The queue is made in the order you dragged it to; anything you haven't placed follows, pages you added first, then oldest first.

## Branding
Everything is editable in the studio's **Settings** tab and stored in `brand.json`. That file is yours and git-ignored: it's created from `brand.example.json` the first time you run anything, so your settings never end up in the repo.
- **Channel:** name, tagline, spoken closing line, hashtags, and a logo (1–2 characters, or an uploaded image)
- **Colours:** background, panel, accent, text and muted text
- **Typography:** any Google Fonts family (searchable, with live samples). The font is downloaded into `brand/fonts/` and fetched again automatically on a fresh clone.
- **Voice:** 25 Kokoro English voices and the speed
- **Music:** upload an MP3 (`brand/music.mp3`) and set its volume under the voice
- **Content:** local LLM, Gemini model and key, minimum interest score, maximum length

New settings apply to new renders. Use **Save & re-render** on a video to update it.

## YouTube upload (one-time)
1. In [Google Cloud Console](https://console.cloud.google.com), create a project, enable **YouTube Data API v3**, and create an OAuth client of type **Desktop app**. Save its JSON as `secrets/client_secret.json`.
2. Under **Google Auth Platform → Branding**, set a homepage and privacy policy URL. This repo's GitHub Pages site works: `https://<user>.github.io/shorts-machine/` and `…/PRIVACY`. Add that domain under *Authorized domains*.
3. Under **Audience**, click **Publish app** (*In production*). In *Testing* mode the sign-in token expires every 7 days.
4. Run the first upload. A browser opens. Choose **Advanced → Go to (app name)**, since this is your own unverified app. `secrets/token.json` is then saved and later runs don't prompt.

Videos upload **private**, or scheduled with `--publish-at`. Until the project passes YouTube's API audit, YouTube keeps API uploads private. Quota allows about 6 uploads a day (deleting costs 50 units). The first `unpublish` asks Google once for the extra "manage videos" permission.

## Automation
In the studio go to **Settings → Automation**: turn it on, pick up to 4 times a day and the weekdays, set a topic or link to find ideas in when the ideabank is empty (empty: random Wikipedia), and choose whether to upload. Each run publishes either the top of your **Queue** or the video it just made, kept private or public at a set time at least 2 hours later, so you can review it. **Save** installs a macOS launchd job (`~/Library/LaunchAgents/com.shortsmachine.auto.plist`); turning it off removes the job.
- It runs `./rw auto` even when the studio is closed. If the Mac is asleep at that time, it runs when it wakes. Keep Ollama running.
- Output goes to `data/auto.log`, and the last runs are shown in Settings. A failed run shows a macOS notification. Overlapping runs are skipped.
- `./rw auto --dry-run` shows what the next run would do. `./rw schedule` applies the `brand.json` schedule from the command line.

## Files
| Path | What |
|---|---|
| `rw.py` | the pipeline and CLI |
| `sources.py` | non-Wikipedia sources: web, RSS, Hacker News, arXiv, APOD |
| `web.py`, `web.html` | the studio (Python standard library server and a single page) |
| `rw` | wrapper that runs `rw.py` with the project's own Python |
| `brand.example.json` | default settings; copied to your own `brand.json` (git-ignored) on first run |
| `setup.sh` | builds the local toolchain |
| `PRIVACY.md` | privacy policy (served by GitHub Pages for the OAuth screen) |
| `brand/` | fonts, logo, music (git-ignored) |
| `data/randomwiki.db` | page log (git-ignored) |
| `work/<pageid>/` | images, voice, `meta.json` with script, title, description and credits (git-ignored) |
| `out/NNN-slug.mp4` | finished videos (git-ignored) |
| `secrets/` | OAuth files (git-ignored) |

## Licences of what gets used
Wikipedia text is CC BY-SA 4.0. Images come from Wikimedia Commons and Openverse (CC/public domain), NASA (public domain) and The Met (CC0), and each one is credited in the YouTube description, along with the text sources. Fonts come from Google Fonts (SIL OFL or Apache, with the licence saved next to each font). The Kokoro voice model is Apache-2.0.
