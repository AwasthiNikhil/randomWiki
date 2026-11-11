# Privacy Policy — Random Wiki Shorts

_Last updated: 2026-11-11_

Random Wiki Shorts is a personal, self-hosted tool that turns Wikipedia articles into short videos and uploads them to its owner's own YouTube channel.

**What it accesses.** The app requests one Google permission, `youtube.upload`, so it can upload videos to the YouTube channel of the person who signs in. It does not read, change or delete any other YouTube or Google account data.

**What it stores.** The OAuth token from Google is saved only on the computer running the app (`secrets/token.json`). It is never sent anywhere except to Google's own APIs. The app keeps no analytics, collects no personal data and shares nothing with third parties.

**Other services it uses.** The app reads public content from Wikipedia, Wikimedia Commons and Openverse. No user data is sent to these services.

**Revoking access.** You can remove the app's access at any time at https://myaccount.google.com/permissions, and delete `secrets/token.json`.

**Contact.** Open an issue at https://github.com/AwasthiNikhil/randomWiki/issues.
