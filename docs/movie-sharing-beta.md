# Movie Sharing beta quick start

The full **3D Blu-ray to Vision Pro** Mac app converts movies and shares completed files. **Shiny 3D Cinema** on Vision Pro lets you browse the shared folder and choose what to play.

Use the latest published **3D Blu-ray to Vision Pro** Mac beta, **0.3.3b2 (173) or later**, with **Shiny 3D Cinema 0.1.0 (3)**. The Mac app requires Apple Silicon and macOS 26 or later; the player requires visionOS 26 or later.

1. On the Mac, open **Settings → Updates**, set **Update route** to **Beta**, then choose **Check for Updates…**. If this is your first Mac install, download the DMG for the newest published beta from [Mac releases](https://github.com/cbusillo/BD_to_AVP/releases) and install the app. Keep Beta selected for subsequent updates.
2. On Vision Pro, install **Shiny 3D Cinema** through [TestFlight](https://testflight.apple.com/join/2qERGYYg).
3. Put a small completed MOV, MP4 or M4V movie in a folder on the Mac. For a common starting point, download the [side-by-side sample](../macos/BDToAVPPlayer/Resources/Stereo-Check-SBS.mov) or [over-under sample](../macos/BDToAVPPlayer/Resources/Stereo-Check-OU.mov). Both synthetic samples last 45 seconds and are intentionally silent.
4. In the Mac app's toolbar, open **Movie Sharing**, choose **Add Folder…**, select that folder, and enable **Share movies on this Mac**. Keep the Mac app open and the Mac awake.
5. Connect both devices to the same trusted network. In Shiny 3D Cinema, choose **Mac Movies**, then **Find Macs**. Allow Local Network access when prompted and select your Mac.
6. Compare the pairing code on both devices and choose **Codes Match** on each. Choose a movie from the headset's list to begin playback.

Check depth and eye order, pause and seek, then use **Done** to return to the library. Open the same movie again to check resume. Quit and reopen the Mac app, confirm sharing is enabled, and reconnect from the headset. With one of your own completed movies, also check audio and subtitle selection when tracks are present.

This Movie Sharing beta supports compatible MV-HEVC and full side-by-side or over-under SDR HEVC movies. Complete a conversion on the Mac before sharing its output; direct ISO/BDMV playback is a separate workstream.

If the Mac is missing, check the network, Local Network permission, sharing switch and whether the Mac is awake. If the folder is unavailable, reconnect its drive or choose the folder again on the Mac. Use **Refresh** after adding a movie. To remove a pairing, use **Forget** under Paired Devices on the Mac and **Forget This Mac** on the headset, then pair again.

The player's **On My Vision Pro** section and bundled **Start SBS Check** / **Start Over-Under Check** samples also work without the Mac. That is a useful first check if Mac sharing is unavailable.

Send player feedback through TestFlight. Include both app versions, device OS versions, the movie format and the steps that failed; do not attach private movie content. Mac conversion or updater problems can be reported through the Mac app's existing support flow.
