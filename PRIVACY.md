# Privacy Policy — Shorts Machine

_Last updated: 2026-11-11_

Shorts Machine is a personal, self-hosted tool that turns Wikipedia articles into short videos and uploads them to its owner's own YouTube channel.

**What it accesses.** The app requests the Google permission `youtube.upload` to upload videos to the YouTube channel of the person who signs in. Only if the owner chooses to delete one of the app's uploaded videos does it also request `youtube.force-ssl`, which it uses solely to delete that video. It does not read or change any other YouTube or Google account data.

**What it stores.** The OAuth token from Google is saved only on the computer running the app (`secrets/token.json`). It is never sent anywhere except to Google's own APIs. The app keeps no analytics, collects no personal data and shares nothing with third parties.

**Other services it uses.** The app reads public content from Wikipedia, Wikimedia Commons, Openverse, NASA Image Library, The Metropolitan Museum of Art and Google Fonts. No user data is sent to these services.

**Revoking access.** You can remove the app's access at any time at https://myaccount.google.com/permissions, and delete `secrets/token.json`.

**Contact.** Open an issue at https://github.com/AwasthiNikhil/shorts-machine/issues.
