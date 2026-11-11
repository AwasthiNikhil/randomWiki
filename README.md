# Shorts Machine

Articles go in, and branded YouTube Shorts come out. Wikipedia is the main source; any web article, RSS/Atom feeds, Hacker News, arXiv and NASA's Astronomy Picture of the Day also work. Everything runs locally and costs nothing.

1. **Pick:** random articles, or ideas found around a topic through its Wikipedia link network (1–3 hops). Stubs, lists and disambiguation pages are dropped, and a local LLM scores the rest out of 10.
2. **Log:** every page that comes up is stored in SQLite with a status and the reason, so nothing is ever processed twice.
3. **Script:** Ollama writes a 45–55s narration from the full article, then fact-checks it against the article. You can edit it before rendering again.
4. **Visuals:** the article's own free-licensed images, topped up from a Wikimedia Commons search. You can also pick from Openverse, NASA and The Met, or upload your own.
5. **Voice:** Kokoro TTS. Its per-word timestamps drive word-highlighted captions, and Roman numerals are read correctly ("World War Two").
6. **Render:** a branded 1080×1920 layout drawn with Pillow and encoded with ffmpeg.
7. **Score:** each video gets a 0–100 potential score from topic demand, hook, appeal, reach, visuals and length.
8. **Publish:** YouTube Data API upload, private or scheduled, best potential first. Credits go into the description automatically.

## Sources
| Source | Find ideas | What the script is written from | Images |
|---|---|---|---|
| Wikipedia | random, or a topic's link network (1–3 hops) | the article | the article's own, then Commons |
| Any web page | paste a link | the page's main text (extracted with trafilatura; robots.txt respected) | Commons/Openverse search, never the page's own images |
| RSS / Atom | the latest items of a feed URL | each item's linked page | as web |
| Hacker News | today's top stories | the linked article, or the post text | as web |
| arXiv | papers matching a topic | the abstract (arXiv metadata is CC0) | Commons search, else a title card |
| NASA APOD | the latest pictures (via NASA's RSS feed) | the explanation | search; APOD photos are often copyrighted, so they're not reused automatically |

Credits name the source and link back. Non-Wikipedia text is copyrighted by its authors: the narration is a short summary in new words, so review it before publishing.

```
./rw run --url https://news.ycombinator.com/item?id=8863     # any supported link works
./rw discover --source hackernews --count 5
./rw discover --source rss --feed https://www.nasa.gov/feed/
./rw discover --source arxiv --topic "dark matter"
./rw discover --source apod
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
./setup.sh          # about 1.5 GB download, ends with "selftest ok"
./rw web            # opens the studio at http://127.0.0.1:8765
```
The voice model (about 300 MB) downloads on the first render.

## Studio (`./rw web`)
A local web UI. It's bound to 127.0.0.1 and runs the same commands as the CLI, streaming their output into a terminal panel.
- **Episodes:** previews you can search, filter and sort by potential. Open one to watch it, edit its title, narration and visuals, then **Save & re-render**. You can also upload it, skip it, delete it, or delete it from YouTube.
- **Queue:** what gets made next. Drag to reorder, paste links in bulk, or **Find ideas** randomly or around a topic, with a degree-of-relation control.
- **Page log:** every page seen, with reasons. Sortable and searchable, with multi-select delete. Uploaded pages are always kept.
- **Settings:** channel, colours, Google Fonts, voice, music, LLM and thresholds, with a real-frame preview.

## CLI
```
./rw run                      # make the next video in the queue, else discover a random page
./rw run --url <wiki url>     # make a specific page now (also retries a failed one)
./rw add <url> [<url> ...]    # queue pages for later runs
./rw discover [--count 5] [--topic nepal --degree 1|2|3|any]   # queue ideas: random, or from a topic's link network
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
Everything is editable in the studio's **Settings** tab and stored in `brand.json`:
- **Channel:** name, tagline, spoken closing line, hashtags, and a logo (1–2 characters, or an uploaded image)
- **Colours:** background, panel, accent, text and muted text
- **Typography:** any Google Fonts family (searchable, with live samples). The font is downloaded into `brand/fonts/` and fetched again automatically on a fresh clone.
- **Voice:** 25 Kokoro English voices and the speed
- **Music:** upload an MP3 (`brand/music.mp3`) and set its volume under the voice
- **Content:** local LLM, minimum interest score, maximum length

New settings apply to new renders. Use **Save & re-render** on a video to update it.

## YouTube upload (one-time)
1. In [Google Cloud Console](https://console.cloud.google.com), create a project, enable **YouTube Data API v3**, and create an OAuth client of type **Desktop app**. Save its JSON as `secrets/client_secret.json`.
2. Under **Google Auth Platform → Branding**, set a homepage and privacy policy URL. This repo's GitHub Pages site works: `https://<user>.github.io/shorts-machine/` and `…/PRIVACY`. Add that domain under *Authorized domains*.
3. Under **Audience**, click **Publish app** (*In production*). In *Testing* mode the sign-in token expires every 7 days.
4. Run the first upload. A browser opens. Choose **Advanced → Go to (app name)**, since this is your own unverified app. `secrets/token.json` is then saved and later runs don't prompt.

Videos upload **private**, or scheduled with `--publish-at`. Until the project passes YouTube's API audit, YouTube keeps API uploads private. Quota allows about 6 uploads a day (deleting costs 50 units). The first `unpublish` asks Google once for the extra "manage videos" permission.

## Automation
In the studio go to **Settings → Automation**: turn it on, pick up to 4 times a day and the weekdays, choose where ideas come from when the queue is empty, and choose whether to upload (kept private, or public at a set time at least 2 hours after the video is made, so you can review it). **Save** installs a macOS launchd job (`~/Library/LaunchAgents/com.shortsmachine.auto.plist`); turning it off removes the job.
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
| `brand.json` | channel identity and settings |
| `setup.sh` | builds the local toolchain |
| `PRIVACY.md` | privacy policy (served by GitHub Pages for the OAuth screen) |
| `brand/` | fonts, logo, music (git-ignored) |
| `data/randomwiki.db` | page log (git-ignored) |
| `work/<pageid>/` | images, voice, `meta.json` with script, title, description and credits (git-ignored) |
| `out/NNN-slug.mp4` | finished videos (git-ignored) |
| `secrets/` | OAuth files (git-ignored) |

## Licences of what gets used
Wikipedia text is CC BY-SA 4.0. Images come from Wikimedia Commons and Openverse (CC/public domain), NASA (public domain) and The Met (CC0), and each one is credited in the video and the description. Fonts come from Google Fonts (SIL OFL or Apache, with the licence saved next to each font). The Kokoro voice model is Apache-2.0.
