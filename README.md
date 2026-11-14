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
| nothing | the next idea in the ideabank, else a new one from your channel topics (see **Ideas** below) |

**Questions get answered, or no video.** Two checks on the local model:
1. Before writing, it must quote the sentence in the research that answers the question; the quote is checked against the notes and judged on its own. If web research (Gemini or Claude) has none, Wikipedia research by the local model is tried next. If neither has one, the video fails with "the research didn't find an answer" rather than circle the topic.
2. After writing, it must quote the narration sentence that gives the answer, judged the same way. If none does, the script is rewritten around the answer (twice at most), else the video fails with the reason.

## Ideas: your topics and what's trending in them
Set your **channel topics** in **Settings → Ideas & videos** (for example *space, history, food science*). When the ideabank is empty (an empty **Make now**, **Find ideas** with no topic, or a scheduled run), new ideas come from, by default, **a mix**:
- **Evergreen:** the local model brainstorms fresh angles on one of your topics.
- **Trending:** this week's Google News headlines in your topics plus today's Google Trends (no key needed). The local model turns the promising ones into a question about the lasting idea behind the news ("How do astronomers spot the earliest quasars?"), and labels what each headline is about. Sports results, politics, crime, war, disasters, celebrity and business stories are dropped in code, as are ideas that don't squarely fit your topics.

You can also choose only one of those, or random Wikipedia pages. Either way, ideas are researched when they're made. `./rw discover --source trending` finds trending ideas on demand.

## Where the facts come from
For a question or topic, research comes from sources actually read, never from the model's memory: Gemini or Claude
research the web if one is chosen in Connectors; otherwise the app searches the web itself (no key needed), reads a
few pages from different sites and writes from those; Wikipedia is used only if they don't answer the question.
Settings → Videos has "Wikipedia only" for channels that prefer it. Every page read is linked in the description.

## More models and sources (all free)
- **Writers:** besides the local model, Gemini and Claude, you can add a free key for Groq, OpenRouter or Cerebras in
  Connectors → AI model and choose it for writing. Download more local models there too (any name from
  ollama.com/library).
- **Pictures drawn on your Mac:** Connectors → AI model → Pictures downloads FLUX.1 schnell (9.6 GB, Apache 2.0). Then
  a video's Visuals has "Draw a picture with AI": describe it, wait about a minute, and it is saved in Media → Images.
- **A channel drawn in one style:** Settings → Videos → "Pictures come from" → "Drawn for each video" draws one picture
  per sentence in the style you describe, instead of looking for photographs.
- **Picture libraries:** Unsplash, Smithsonian Open Access and Europeana with your own free key (Connectors → Media
  sources), plus the Cleveland Museum of Art and old public-domain films from the Prelinger Archives with no key.
- **Voices:** Settings → Voice & music → Online voices gives about 300 Microsoft Edge voices in many languages. It is
  free but unofficial and sends the script text to Microsoft.

## AI models: local, Gemini or Claude
Everything runs locally by default (Ollama). In the studio's **Connectors** tab you can add a Gemini key (free at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)) or an Anthropic key for Claude ([platform.claude.com](https://platform.claude.com/settings/keys)), and choose which model writes:
- **Research goes beyond Wikipedia.** Gemini uses Google Search if your key's plan includes it; otherwise it picks reliable pages on any site, reads them itself and writes notes from what it read. Claude uses Anthropic's web search and cites the pages it used. Every source is credited. If the model can't read enough, research falls back to Wikipedia rather than script from the model's memory.
- **Visuals:** the model names the concrete things to show, and their Wikipedia articles supply the images.
- **Writing:** scripts, fact-checks and image picks use the chosen model. Scoring ideas in bulk stays on the local LLM, which is free. If the cloud model fails (quota, busy), the local LLM takes over for the rest of that run.

Keys are yours, not the project's: `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` in your shell are used first. Keys pasted in Connectors are saved to `secrets/` (git-ignored); scheduled runs need those, since they don't load your shell profile. Pick the model there too: `gemini-flash-latest` follows Google's current Flash model; for Claude, Opus 5.5 (default), Sonnet 5.5 or Haiku 5.5. Claude requests let Anthropic answer with another model if one declines (server-side fallback).

**Usage:** each video shows the AI requests and tokens it used (Episodes → open a video), and Connectors shows today's totals per model. Days follow Pacific time, when Google resets the free quota. The API doesn't report how much quota is left; see [ai.dev/rate-limit](https://ai.dev/rate-limit).


1. **Pick:** your ideabank, else new ideas from your channel topics and what's trending in them (see **Ideas**), or random Wikipedia articles if you choose (stubs, lists and disambiguation pages are dropped, and a local LLM scores the rest out of 10). Ideas around a topic: the local LLM brainstorms questions and subjects, closer or further from the topic (degree 1–3). Each idea must name a real Wikipedia article and is checked against its intro, so ideas with a false premise are reworded or dropped. Ideas are researched properly when the video is made.
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

## Auto or manual
**Settings → Videos → When a video is made.** *Auto* researches, writes, picks visuals, voices and renders in one go. *Manual* stops at a **draft** once the script and visuals are ready, so you can shape it before anything is voiced or rendered (`./rw run --auto "…"` skips the stop for one video). Open the draft (Episodes) and:
- **Script:** edit the title and narration, or record it in your own voice.
- **Visuals:** search Commons, Openverse, NASA, the Met, WordPress Photos, Pexels, Pixabay or your library, upload your own, drag to reorder. Click a visual to set:
  - **when it appears** (which sentence) and its **motion** (zoom in, zoom out, pan left, pan right, still);
  - **callouts**: up to 4 arrows, circles and labels, placed by clicking the picture. Arrows draw in, circles trace round, labels pop in, after the delay you choose;
  - a **sound effect** as it appears: upload one, or search free ones (Freesound, via Openverse).
- **Sound:** the channel music, no music, or a track just for this video: upload one, or search free music (Jamendo, ccMixter and others, via Openverse), with its own volume.

Then click **Render video**. The same controls stay available on rendered videos with **Save & re-render**.

Free music and sounds are limited to licences that allow reuse under a narration (public domain, CC0, CC BY, CC BY-SA; never "no derivatives" or "non-commercial"), and each one used is credited in the video's description.

## Stock media
In **Connectors → Stock media**, add a free key for **Pexels** and/or **Pixabay** (or set `PEXELS_API_KEY` / `PIXABAY_API_KEY`). Their photos and videos then appear in the editor's search, and auto-pick falls back to them (and to the keyless WordPress Photo Directory) when Wikipedia, Commons and Openverse have nothing for a sentence. Keys are saved in `secrets/stock.json`.

## Media: download video, audio and images
Keep references, clips, music and pictures in `media/` (`video/`, `audio/`, `image/`), separate from what you make (git-ignored). The studio's **Media** tab has a section for each; paste links (one per line), or run `./rw download URL [URL ...] [--kind video|audio|image] [--section 1:20-1:45]`.
- **Video** and **audio** use yt-dlp: YouTube, Vimeo, X, Reddit, TikTok, Instagram, SoundCloud, Bandcamp, Internet Archive and [many more sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), plus Wikimedia Commons pages through Commons' own API. Video is saved as MP4 (up to 1080p); audio as M4A, sound only. Both can keep just a part (`1:20-1:45`).
- **Images:** a direct image link, a Wikimedia Commons file page (with its licence), or any page's main image (the one it shows when shared). Galleries and posts with several images (Instagram carousels, Reddit galleries, Flickr albums) aren't supported yet; [gallery-dl](https://github.com/mikf/gallery-dl) would add them.
- **Upload from device** in each section adds your own files: video is kept as MP4 (repackaged instantly when it's already H.264, converted otherwise), audio becomes M4A, images JPEG or PNG. Your own uploads get no credit line when used.
- Each file has a `.json` next to it with the title, uploader, source link and licence when stated, so it can be credited.
- **Use in a video:** a clip or image is added to the video's visuals; audio becomes the video's music. In a video's editor you can also search **My media**. Whatever you use is credited in the description.
- Search each section by title, uploader, site or licence. Nothing else is kept: no download history, and yt-dlp's cache is off.
- Use other people's work only with permission, under its licence (Creative Commons, public domain), or as fair use.

## Keeping track of media
Every item in the Media tab says where it is used ("Used in Rank Them EP 006", "Spare for…", or "Not used yet").
Clips fetched automatically for clip videos are kept apart from what you added yourself: the tab opens on "Added by
you", with "Fetched for clip videos" and "All" beside it, a "Not used yet" tick, and one button to delete fetched clips
that no video uses. Deleting something a video uses asks twice, because that video could no longer be re-rendered.

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

**Find ideas** in the Ideabank takes a topic (the local LLM brainstorms ideas around it), a link, or nothing (new ideas from your channel topics and what's trending in them). A link (`./rw discover --topic URL`) is worked out what you pasted: an RSS/Atom feed, a site or blog (its feed is found automatically), a subreddit (link posts are read from the article they point to), a Wikipedia `Category:` or `List of…` page (its articles), a Wikipedia article (explores around it), a Hacker News list, an arXiv listing, a single article, or any page that lists articles (news sections, blog indexes). Automation can use a link as its source when the ideabank is empty.

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
- **Episodes:** previews you can search, filter and sort by potential. Open one to watch it, edit its title, narration and visuals, then **Save & re-render**. You can also publish it, skip it, delete it, or delete it from YouTube.
- **Queue:** videos ready to upload, in the order they'll go out. Drag to reorder; videos you haven't placed are sorted by potential score. **Upload pending** and scheduled runs take from the top.
- **Ideabank:** what gets made next. Drag to reorder, or **Find ideas** randomly, around a topic (with a degree-of-relation control), or in a link. Add your own with the sidebar's **Save idea**; several links, one per line, become one idea each.
- **Log:** every idea seen, with reasons. Sortable and searchable, with multi-select delete. Uploaded pages are always kept.
- **Connectors:** the AI model that writes (local, Gemini, Claude) and where videos are published (YouTube, Facebook, Instagram, TikTok).
- **Settings:** Brand (channel, colours, Google Fonts, with a real-frame preview), Voice & music, Videos (thresholds) and Automation.

Every view has its own address, so refreshing or bookmarking keeps your place: `/ideabank`, `/settings/voice`, `/video/<id>` and so on.

It stays quick with thousands of videos: lists show 60 at a time and load more as you scroll, cards use small poster images (`out/.posters/`, made once per video), the list data is trimmed and gzipped, and a video's full details load only when you open it.

## Several channels
The menu next to the channel name switches between channels and has "New channel…". Each channel has its own brand,
voice, topics, ideabank, videos, schedule and platform sign-ins (connect YouTube once per channel and pick that channel
on Google's screen). AI keys, stock keys, the YouTube client file, fonts and the Media tab are shared. The studio shows
one channel at a time and restarts itself when you switch (not while a command is running); scheduled runs happen in
the background for every channel whichever one is open, taking turns if they fall on the same minute. Your first
channel lives in the project folder itself, later ones in `workspaces/<name>/`; deleting a channel means deleting its
folder (turn its schedule off first). On the command line, `RW_WORKSPACE=<name> ./rw …` works on that channel.

## Quizzes
Set a channel to **Quizzes** in Settings → Videos, then type a topic ("the solar system"). The topic is researched,
five questions are written from what was read, and each one is shown with three options, read out, given three
seconds with a ticking countdown, and answered. A question is only used if a sentence from the research proves its
answer; open the video in the studio to see each question with that sentence. If the video would run over the
channel's maximum length, the last question is left out. Quizzes are made straight away (no edit-first step) and
"Remake" writes a new set. To change a quiz, open it in the studio: edit any question, its three options or
which one is right, remove or add questions, then Save & re-render. A question you change or add is no longer checked
against the research.

## Guess the picture
Set a channel to **Guess the picture**, then type a kind of thing ("famous landmarks", "dog breeds"). Five blurred
photographs clear over four seconds each, with a countdown, then the name appears and is read out. Only things with a
Wikipedia article whose main picture is a free photograph are used; the name and the line under it are that
article's. A channel doesn't show the same thing twice.

## On this day
Set a channel to **On this day** and click Make now with nothing typed (or type a date, "25 December"). The video
shows a few things that happened on that date, oldest first, each with its year, a picture and the words read out.
Everything comes from Wikipedia's daily "On this day" list; deaths and disasters are left out. With a daily schedule
this makes one video a day without any input.

## Top 5 countdowns
Set a channel to **Top 5 countdowns**, then type what to count down ("tallest mountains on Earth", "facts about
octopuses"). The topic is researched and five entries are shown from number 5 to number 1, each with a picture, its
name and one sentence. An entry is used only if a sentence in the research backs it, numbers included.

## Word of the day
Set a channel to **Word of the day** and click Make now with nothing typed for Wiktionary's word of the day, or type
any word. The video shows the word, then its meaning, origin and an example, one after another as they are read.

## Changing what a video says
Open a guess, on this day, countdown or word video in the studio: every entry's name and words can be changed, and
entries removed; then Save & re-render. Pictures stay as they are.

## Clip countdowns
A channel can make countdowns of short clips instead of narrated explainers: Settings → Ideas & videos → "What this
channel makes". Then the Make box takes words (YouTube Shorts under that hashtag: "funny cats" looks in #funnycats)
and/or links from TikTok, Instagram, Facebook, YouTube or Reddit (one video, a profile or a playlist). The most viewed
clips are joined, number 1 last, with their own sound and a rank badge. Each clip plays whole (only clips of 4 to 30
seconds are taken, as many as fit the channel's maximum video length), clips with captions or a channel stamp on
screen are left out (macOS only), search results must name the subject in their title, and a clip is never used twice
on a channel. "Remove human voices from clips" takes talking, narration and singing out of each clip's sound and
keeps the music and other sounds. Settings also takes a list of accounts to draw from every time (TikTok accounts, YouTube channels or
playlists); Facebook and Instagram can't be browsed, so paste single video links from them. Open a clip video to choose its clips: tick the ones to use, move them into playing order (the last is number 1),
play any of them, or add one by pasting a link; then re-render. A few spare clips are fetched with each video so
there is something to swap in. With "Stop at a draft" (Settings → Videos) nothing is rendered until you have chosen.
For gameplay and other footage that sits inside long videos, the channel's Clips settings have: **Where to look**
(YouTube videos of any length: the most replayed 15 seconds of each long one are taken), **How new** (last day, week
or month), **How a clip fills the screen** (whole over a blurred copy, or zoomed to fill) and **Keep clips with text
on screen** (turn on for games). In a clip video, each clip has "from … to …" seconds to trim it and its own fill
choice (blurred edges, or fill keeping the middle, left or right); to add part of a long video, paste its link and
the part, like 1:20-1:45.
From the command line: `./rw clips "funny cats" --count 5`. These are other people's videos:
each is credited in the description, but credit is not permission, and platforms can remove or demonetise reposts.

## Prompts and themes
Settings → Prompts shows the instructions each channel sends to the AI model: three for writing scripts (from an
article, for a question, from your own text), three for ideas (in your topics, in the news, scoring articles) and one
for choosing what to show. Edit them to change what a channel is about and how it sounds. Under each is its fixed part
(the material and the reply format), which you can read but not change. "Save as theme" stores the current set under a
name (`prompts/<name>.json`) so another channel can load it.

## The same video on another channel
Open a video and use "Copy to channel": the script and pictures (or the clips) are copied and the video is made again
with the other channel's logo, colours, voice and closing line. It lands in that channel's Queue; a channel can hold
each video only once.

## CLI
```
./rw run ["anything"]         # a question, topic, paragraph or link; nothing = the next idea, else a random page
./rw add "anything" [...]     # save ideas for later runs
./rw discover [--count 5] [--topic nepal|URL --degree 1|2|3|any]   # find ideas: random, brainstormed around a topic, or in a link
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
- **Videos:** minimum interest score, maximum length

New settings apply to new renders. Use **Save & re-render** on a video to update it.

## Publishing: YouTube, Facebook, Instagram, TikTok
In **Connectors**, tick every platform a video should go to. **Publish** on a video, **Upload pending** and scheduled runs post to all ticked platforms at once. Each platform that succeeds is remembered; if one fails, the video stays in the Queue and publishing again retries only that one. A video counts as uploaded once every ticked platform has it.

### YouTube
In **Connectors → YouTube**. It links each step straight to the right Google Cloud page:
1. Create a Google Cloud project and enable the **YouTube Data API v3**.
2. Set up **Google Auth Platform** (app name, your email, audience *External*), and under **Audience** click **Publish app**. In *Testing* mode the sign-in expires every 7 days.
3. Create an OAuth client of type **Desktop app** and download its JSON.
4. **Add client file** in Settings, then **Connect YouTube**. Google warns that the app isn't verified: it's your own, so choose **Advanced → Go to (app name)**.

Everything stays in `secrets/` (git-ignored). **Disconnect** revokes the app's access at Google and deletes the saved sign-in. From the command line: put the file at `secrets/client_secret.json` and run `./rw connect`.

Each user needs their own client: Google's quota (about 6 uploads a day) and its limits for unverified apps are per client, so one shared client would be split across everyone.

Videos upload **private**, or scheduled with `--publish-at`. Until the project passes YouTube's API audit, YouTube keeps API uploads private. Quota allows about 6 uploads a day (deleting costs 50 units). The first `unpublish` asks Google once for the extra "manage videos" permission.

### Facebook & Instagram
One Meta connection covers both. Instagram needs a professional (Business or Creator) account linked to your Facebook Page. In **Connectors → Facebook & Instagram**:
1. Create a Meta app (type *Business*). As its admin you can use it with your own Page in development mode.
2. In the Graph API Explorer, add `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `instagram_basic`, `instagram_content_publish` and generate a token for your Page.
3. Extend it in the Access Token Debugger and paste the long-lived token. The studio finds your Page and its Instagram account and keeps a Page token that doesn't expire.

Videos post right away as public Reels with the title and description as the caption. Facebook Reels must be 90 s or shorter; longer videos skip Facebook with a message.

### TikTok
Create an app on TikTok for Developers (*Desktop* platform) with **Login Kit** and **Content Posting API** (`user.info.basic`, `video.upload`) and the redirect URI `http://127.0.0.1:8766/callback/`. Paste its client key and secret in **Connectors → TikTok** and **Connect TikTok** (or `./rw connect tiktok`). Videos are sent to your TikTok inbox; open TikTok to add the caption and post. Posting straight to a profile needs TikTok to audit the app; until then TikTok allows only private posts on a private account.

## Automation
In the studio go to **Settings → Automation**: turn it on, pick up to 4 times a day and the weekdays, set a topic or link to find ideas in when the ideabank is empty (empty: your channel topics and what's trending in them), and choose whether to publish. Each run publishes either the top of your **Queue** or the video it just made, kept private or public at a set time at least 2 hours later, so you can review it. **Save** installs a macOS launchd job (`~/Library/LaunchAgents/com.shortsmachine.auto.plist`); turning it off removes the job.
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
| `data/shorts.db` | page log (git-ignored) |
| `work/<pageid>/` | images, voice, `meta.json` with script, title, description and credits (git-ignored) |
| `out/NNN-slug.mp4` | finished videos (git-ignored) |
| `media/` | video, audio and images you downloaded (`video/`, `audio/`, `image/`), each with a `.json` of its source (git-ignored; was `library/`) |
| `secrets/` | API keys, OAuth files and platform tokens (git-ignored) |
| `workspaces/<name>/` | every channel after the first: its own `brand.json`, `data/`, `work/`, `out/`, `brand/` and sign-ins (git-ignored) |

## Licences of what gets used
Wikipedia text is CC BY-SA 4.0. Images come from Wikimedia Commons and Openverse (CC/public domain), NASA (public domain) and The Met (CC0), and each one is credited in the YouTube description, along with the text sources. Fonts come from Google Fonts (SIL OFL or Apache, with the licence saved next to each font). The Kokoro voice model is Apache-2.0.
