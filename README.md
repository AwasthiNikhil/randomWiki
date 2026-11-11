# Random Wiki Shorts

A random Wikipedia page goes in, and a branded YouTube Short comes out. Everything runs locally and costs nothing.

1. **Pick:** fetch random articles, drop stubs, lists and disambiguation pages, then let a local LLM score the rest out of 10.
2. **Log:** every page that comes up is stored in SQLite with a status and the reason, so nothing is ever processed twice.
3. **Script:** Ollama writes a 45–55s narration from the full article, then fact-checks it against the article.
4. **Visuals:** the article's own free-licensed images (Wikimedia Commons), then Openverse, then a branded text card.
5. **Voice:** Kokoro TTS. Its per-word timestamps drive word-highlighted captions.
6. **Render:** a fixed branded 1080×1920 layout drawn with Pillow and encoded with ffmpeg.
7. **Publish:** YouTube Data API upload, private or scheduled. Credits go into the description automatically.

## Requirements
- macOS or Linux, about 2 GB of disk, `curl` and `bash`
- [Ollama](https://ollama.com) running locally, with the model named in `brand.json` pulled:
  ```
  ollama pull llama3.1:8b
  ```

Everything else (uv, Python 3.12, Python packages, the bundled ffmpeg, fonts, the voice model) is installed **inside this folder** by `setup.sh`. Nothing is installed system-wide.

## Setup
```
git clone git@github.com:AwasthiNikhil/randomWiki.git
cd randomWiki
./setup.sh          # about 1.5 GB download, ends with "selftest ok"
./rw run --url https://en.wikipedia.org/wiki/Emu_War   # first video, written to out/
```
The voice model (about 300 MB) downloads on the first run.

## Usage
```
./rw run                      # next queued page (curated first), else discover a random one
./rw run --url <wiki url>     # make a curated/fixed video now (also retries a failed page)
./rw add <url> [<url> ...]    # queue curated pages for later runs
./rw run --upload [--publish-at 2026-11-12T15:00:00Z]
./rw upload <pageid>          # upload an already rendered video (review it first)
./rw log [N]                  # rejected / queued / rendered / uploaded / failed, with reasons
./rw selftest
```
`--force` re-makes a page that was already rendered or uploaded.

## Branding
`brand.json` sets the channel name, tagline, spoken outro, colours, fonts, voice, LLM, interest threshold (`min_score`) and maximum length.
- To add music, drop a licence-free track at `brand/music.mp3` (e.g. from the YouTube Audio Library). It loops quietly under the voice.
- Voices: `bm_george` (default), `bm_lewis`, `am_michael`, `af_heart` and others. For `a*` voices set `voice_lang` to `a`; for `b*` voices set it to `b`.

## YouTube upload (one-time)
1. In [Google Cloud Console](https://console.cloud.google.com), create a project, enable **YouTube Data API v3**, and create an OAuth client of type **Desktop app**.
2. On the OAuth consent screen, add your Google account as a test user.
3. Save the JSON as `secrets/client_secret.json`. The first upload opens a browser to sign in, and `secrets/token.json` is saved after that.

Videos upload **private**, or scheduled with `--publish-at`. Unverified API projects can only upload private videos until the project passes Google's audit. Quota allows about 6 uploads a day.

## Daily automation
Keep Ollama running and add this with `crontab -e`:
```
0 9 * * * /path/to/randomWiki/rw run >> /path/to/randomWiki/data/cron.log 2>&1
```

## Files
| Path | What |
|---|---|
| `rw.py` | the whole pipeline |
| `rw` | wrapper that runs `rw.py` with the project's own Python |
| `brand.json` | channel identity and settings |
| `setup.sh` | builds the local toolchain |
| `data/randomwiki.db` | page log (git-ignored) |
| `work/<pageid>/` | images, voice, `meta.json` with title, description and credits (git-ignored) |
| `out/NNN-slug.mp4` | finished videos (git-ignored) |
| `secrets/` | OAuth files (git-ignored) |

## Licences of what gets used
Wikipedia text is CC BY-SA 4.0 and each image's licence is credited in the description. The fonts (Anton, Inter) are SIL OFL and the Kokoro model is Apache-2.0.
