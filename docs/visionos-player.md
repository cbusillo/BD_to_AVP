# Shiny 3D Cinema

**Shiny 3D Cinema** (`BDToAVPPlayer` in Xcode) is the standalone visionOS 26
application for browsing and playing finalized 3D movies on Apple Vision Pro. It is separate from the macOS
converter and from `SpatialPlaybackProbe`,
which remains the qualification-only validator.

## Product Scope

- One plain SwiftUI window contains a split-view library, modal movie details,
  and the player.
- The sidebar's **Library** section holds **On My Vision Pro**; its **Locations**
  section holds the network shares the wearer added (see [Locations](#locations)).
  **On My Vision Pro** opens the local **Your movies** collection with
  Posters and Files modes, format filtering, title or
  filename sorting, 16:9 source-frame thumbnails, and a typography-first
  fallback when a frame cannot be generated.
- Playable movies expose a visible direct Play action in both library modes.
  Selecting the movie content opens a compact modal Details view with source
  status, missing-source recovery, removal, technical metadata, and one
  prominent Play action that remains visible without scrolling.
- Supported `.mov`, `.mp4`, and `.m4v` files, ISOs, MKVs and disc folders
  already present in the app's Documents directory are indexed on launch. File Sharing and opening documents
  in place are enabled for this source path.
- Library metadata and resume positions are stored as bounded JSON under the
  app's Application Support directory. Library media records omit source
  filesystem URLs, but bookmark blobs necessarily encode the source location so
  the app can regain security-scoped access.

## Private internal TestFlight delivery

The visionOS bundle is `com.shinycomputers.bd-to-avp.player`, using team
`MM5YXC7T6E`. The App Store Connect record is **Shiny 3D Cinema** (6811956508).

The public app name is separate from its established engineering identity. Keep
the bundle ID, Xcode target/module, repository and package names, saved-data paths,
Keychain identities and URL schemes stable when changing
public branding. Update the App Store name, `CFBundleDisplayName`, `CFBundleName`,
visible UI/permission strings, current beta notes and privacy policy together.
Changing bundled branding requires a new build number and upload; editing only
the App Store name does not update an installed binary. See
[Apple's display-name instructions](https://developer.apple.com/library/archive/qa/qa1823/_index.html).

**AVP Internal** is the owner-only group; automatic distribution remains off.
The player uses no cryptography beyond what Apple's system frameworks provide; its
plist declares no non-exempt encryption. Reassess that
configuration if cryptographic dependencies or capabilities change.

Use an Xcode release currently accepted by App Store Connect, verified against
[Apple's release notes](https://developer.apple.com/help/app-store-connect/release-notes/).
A locally successful archive does not establish upload eligibility. Select that
installation for these commands with `BD_TO_AVP_XCODE_DEVELOPER_DIR`; this does not
change the machine's global Xcode selection.

Set and commit `MARKETING_VERSION` and `CURRENT_PROJECT_VERSION` for `BDToAVPPlayer`
in `macos/project.yml`, then build from that clean checkout. Use a higher build
number than any previously accepted upload. Keep the source commit, Xcode build number, archive and dSYMs,
export log and IPA SHA256 together with the delivery record.

```sh
BD_TO_AVP_XCODE_DEVELOPER_DIR=/absolute/path/to/supported/Xcode.app/Contents/Developer
DEVELOPER_DIR="$BD_TO_AVP_XCODE_DEVELOPER_DIR" /usr/bin/xcodebuild -version
git rev-parse HEAD
uv run python scripts/native_app.py generate
DEVELOPER_DIR="$BD_TO_AVP_XCODE_DEVELOPER_DIR" /usr/bin/xcodebuild archive \
  -project macos/BluRayToVisionPro.xcodeproj \
  -scheme BDToAVPPlayer -configuration Release \
  -destination 'generic/platform=visionOS' \
  -derivedDataPath build/testflight/DerivedData \
  -archivePath build/testflight/BDToAVPPlayer.xcarchive \
  -allowProvisioningUpdates
DEVELOPER_DIR="$BD_TO_AVP_XCODE_DEVELOPER_DIR" \
  PATH=/usr/bin:/bin:/usr/sbin:/sbin /usr/bin/xcodebuild -exportArchive \
  -archivePath build/testflight/BDToAVPPlayer.xcarchive \
  -exportPath build/testflight/export-internal \
  -exportOptionsPlist macos/TestFlightInternalExportOptions.plist \
  -allowProvisioningUpdates
```

The export options restrict the build to internal testing. The command-local
system PATH keeps Apple's copy tools paired with Apple's rsync during export.
For an authorized upload, copy the export options to the ignored build directory
and change only `destination` to `upload`, then export the same reviewed archive:

```sh
cp macos/TestFlightInternalExportOptions.plist build/testflight/ExportOptions-Upload.plist
/usr/libexec/PlistBuddy -c 'Set :destination upload' build/testflight/ExportOptions-Upload.plist
DEVELOPER_DIR="$BD_TO_AVP_XCODE_DEVELOPER_DIR" \
  PATH=/usr/bin:/bin:/usr/sbin:/sbin /usr/bin/xcodebuild -exportArchive \
  -archivePath build/testflight/BDToAVPPlayer.xcarchive \
  -exportPath build/testflight/upload-internal \
  -exportOptionsPlist build/testflight/ExportOptions-Upload.plist \
  -allowProvisioningUpdates
```

Wait for App Store Connect processing and attach the exact build to **AVP
Internal**. An upload rejected for an unsupported SDK/Xcode requires a new archive
from an accepted toolchain; retrying the old archive cannot fix it. Do not dispatch
the Mac Stable/Prerelease workflows for this.

To withdraw a beta, open its build in App Store Connect's TestFlight tab and use
**Expire Build**, which prevents further tester installation. Record the affected
version/build and reason, and select a known-good unexpired build for the internal
group when available. See [Apple's withdrawal procedure](https://developer.apple.com/help/app-store-connect/test-a-beta-version/stop-testing-a-build/).

Publish the compatible Mac companion with the repository's `publish-current`
command. Keep its stable Current link and the commit-addressed build metadata;
do not replace the production-signed app in `/Applications`.

Use the internal build for wearer acceptance: install from TestFlight, pair,
select a completed movie on Vision Pro, verify both eyes and audio, seek forward
and backward, exercise audio/subtitles and eye order where supported, close and
resume, then test Mac restart and Forget. Automated builds and decoded-frame
checks do not establish physical stereo presentation or sustained playback.

## External TestFlight delivery

An internal-only upload cannot be converted to external testing. Increment the
player's build number, commit the change, and create a fresh archive using the
supported toolchain and archive command above. Export with
`macos/TestFlightExternalExportOptions.plist`, which permits external review:

```sh
DEVELOPER_DIR="$BD_TO_AVP_XCODE_DEVELOPER_DIR" \
  PATH=/usr/bin:/bin:/usr/sbin:/sbin /usr/bin/xcodebuild -exportArchive \
  -archivePath build/testflight/BDToAVPPlayer.xcarchive \
  -exportPath build/testflight/export-external \
  -exportOptionsPlist macos/TestFlightExternalExportOptions.plist \
  -allowProvisioningUpdates
cp macos/TestFlightExternalExportOptions.plist build/testflight/ExportOptions-External-Upload.plist
/usr/libexec/PlistBuddy -c 'Set :destination upload' build/testflight/ExportOptions-External-Upload.plist
DEVELOPER_DIR="$BD_TO_AVP_XCODE_DEVELOPER_DIR" \
  PATH=/usr/bin:/bin:/usr/sbin:/sbin /usr/bin/xcodebuild -exportArchive \
  -archivePath build/testflight/BDToAVPPlayer.xcarchive \
  -exportPath build/testflight/upload-external \
  -exportOptionsPlist build/testflight/ExportOptions-External-Upload.plist \
  -allowProvisioningUpdates
```

Verify the signed export and keep its archive, symbols, commit, toolchain and
checksum with the delivery record. In App Store Connect, add the processed build
to **AVP External**, supply beta description/feedback and App Review contact
details, privacy policy, and concrete testing notes. Submit it for TestFlight
App Review. The first external build requires Apple's approval; a successful
upload alone does not make the build installable by external testers. See
[Apple's external testing procedure](https://developer.apple.com/help/app-store-connect/test-a-beta-version/invite-external-testers).

Invite only the user-selected audience: specific email addresses or an explicitly
requested public link. The existing internal group stays separate. Record review
status, group access and the final invitation mechanism with the exact build.

Reviewers can test the standalone player without an account, Mac, or downloaded
movie: choose **On My Vision Pro** in the sidebar, then **Start SBS Check** and
**Start Over-Under Check**. Both bundled synthetic samples are 45 seconds long and
silent. **Add Location** in the sidebar opens the system folder picker for a
folder or network share holding the reviewer's own supported movies. Include the
matching signed Mac release link in TestFlight's beta description and review notes
once it is published; a developer's local Current build is not a public Mac release.

### Point the TestFlight text at each new Mac Beta

After every published Mac Beta, update the player's TestFlight text so it names
the new Mac build. Use the App Store Connect record identified in
[Private internal TestFlight delivery](#private-internal-testflight-delivery).
Two fields carry the Mac version, release link and guide link:

- the en-US `betaAppLocalizations` `description`
- the `betaAppReviewDetails` `notes`

The change is two substitutions in each field: the Mac version and build in the
form `0.3.3 beta N (build B)`, and the `v0.3.3-beta.N` tag in the release and
guide URLs.

Use the App Store Connect API. Sign an ES256 JWT with the team API key
(`aud: appstoreconnect-v1`, lifetime at most 20 minutes). Read the current values
with `GET /v1/apps/{appId}/betaAppLocalizations` and
`GET /v1/apps/{appId}/betaAppReviewDetail`. Write them with
`PATCH /v1/betaAppLocalizations/{localizationId}` and
`PATCH /v1/betaAppReviewDetails/{appId}`, then read both fields back. The API
key is not in this repository; its location is recorded in the owner's private
infrastructure documentation. Never print the key or its identifiers into logs,
issues or pull requests.

The sidebar's **Privacy Policy** link opens the policy applicable to the build.
Its URL is pinned to the policy's published commit so branch deletion cannot
break it. Use the same URL in TestFlight metadata. Update the
[policy](visionos-player-privacy.md) and its link together when data handling
changes. Apple requires an accessible policy in the app and its metadata under
[the App Review Guidelines](https://developer.apple.com/app-store/review/guidelines/#privacy).

## Locations

**Locations** in the sidebar replace adding movies one at a time (Director decision on
[#711](https://github.com/cbusillo/BD_to_AVP/issues/711#issuecomment-6402562984)).
The wearer adds a network share once, such as a NAS or a Mac running macOS File
Sharing, and it becomes its own library.

visionOS apps cannot mount SMB shares themselves. The supported route is the system's:

1. In the Files app, tap **More**, choose **Connect to Server**, enter the server's
   name or address (for example `nas.local`, or the Mac's name), and sign in as a
   guest or registered user. The server then appears in the Files sidebar and under
   **Recent Servers** for reconnecting
   ([Apple Vision Pro User Guide](https://support.apple.com/guide/apple-vision-pro/tan7acdaa0e3/visionos)).
2. In Shiny 3D Cinema, choose **Add Location** and pick the share, or a folder in it,
   in the system folder picker.

The app keeps a bookmark of that folder, so the Location and its access survive
relaunches. Each title in a Location is reached through the folder's bookmark and its
path inside the folder; titles have no bookmarks of their own. Location records and
what each file held when last inspected are stored in `locations.json` under
Application Support, so reopening a Location reads only new or changed files.

A Location lists, from up to four folder levels, Blu-ray 3D ISOs, disc folders that
hold `BDMV`, MKVs that keep the MVC 3D track, and MV-HEVC (or side-by-side and
over-under HEVC) movies. Each file is inspected the same way as a local source; files
the player cannot play, such as encrypted or 2D discs and MKVs without MVC, are listed
under a collapsed "can't play here" note with the reason. Titles play through the
same players: Blu-ray 3D on the headset (see [Native Blu-ray Video](#native-blu-ray-video)),
everything else through the MV-HEVC player. An open Location looks again every minute,
and **Refresh** looks right away.

When the share cannot be reached, because the server is off or Files disconnected it,
the Location says so and offers **Try Again**, **Choose Folder Again** and **Remove
Location**. iOS-family bookmarks to network volumes can stop resolving after the volume
is unmounted and remounted (Apple bug r.102995804, discussed on the
[developer forums](https://developer.apple.com/forums/thread/774114)); choosing the
folder again replaces the bookmark and keeps the Location. A share that stops answering
is reported unreachable after 45 seconds instead of hanging the library; a request it
gave up on keeps its folder access until it really ends, and no new request starts for
that Location until then, so retries cannot pile up on a stalled share. A large share
is read for up to 15 seconds or 1,000 titles, checked before every entry; the Location
then says it stopped looking before reaching every folder and suggests adding a folder
closer to the movies. A disc folder counts as changed when any file in `BDMV`, its
`PLAYLIST`, `CLIPINF`, `STREAM` or `STREAM/SSIF` folders changes name, size or date.

## Source Access

Local files receive persistent bookmark data. Playback resolves the bookmark
and keeps one balanced security-scoped access lease open for the complete player
session. The lease closes when playback finishes, preparation is replaced, or
the session is destroyed. Bookmark resolution, provider access, and existence
probing run outside the main actor, so a slow Files or SMB-backed provider cannot
freeze the window before playback's loading state appears.

Files in the app's Documents directory use the same library and bookmark path as
movies added before Locations existed. If a source moves or disappears, the details view reports
that state and offers retry or locate recovery instead of treating a temporary
provider outage as deletion. A successful locate preserves the stable library
identity while refreshing its filename, detected format, and bookmark only after
inspection succeeds.

Playback preparation distinguishes opening the source from preparing the media.
The source-opening state is indeterminate and cancellable because third-party
File Providers do not guarantee portable download percentages. Recoverable
failures expose **Try Again**, **Locate**, and **Done** in both the RealityKit
ornament and packed-stereo AVKit actions. Retry starts a fresh bounded session
generation; Locate updates the existing item and then prepares it again. Done
invalidates the active generation, closes any acquired lease, and prevents late
provider completion from installing an item or starting playback.

## Playback Contract

The player supports MV-HEVC plus explicitly identified HEVC side-by-side and
over-under movies. MV-HEVC detection uses AVFoundation stereo multiview playback
characteristics; an HEVC codec tag alone is not accepted as proof of MV-HEVC.
Packed stereo detection prefers embedded packing metadata and otherwise accepts
conservative, separator-delimited filename tokens such as `SBS`, `FSBS`, `OU`,
or `FOU`. These tokens are treated as full-resolution packing; half-resolution
markers such as `HSBS`, `HOU`, `Half-SBS`, and `H-OU` remain unsupported because
they require anamorphic aspect reconstruction. The packed-stereo compositor is currently
qualified for SDR HEVC only. It does not infer stereo from an unusually wide or
tall frame alone.

`MVHEVCPlayerSession` owns one `AVPlayer`, one active `AVPlayerItem`, one
RealityKit entity for MV-HEVC, and one source lease. MV-HEVC playback uses a
`VideoPlayerComponent` configured for stereo viewing, screen spatial video mode,
and portal immersive viewing mode. Packed stereo uses `AVPlayerViewController`,
matching AVFoundation's custom spatial-compositor presentation path. AVKit
supplies play/pause, seeking, the scrubber and time display, audio selection,
and subtitle selection. Eye Order and Done use AVKit's visionOS-native
`contextualActions`, so they remain part of the same supported playback control
surface instead of relying on an overlay beneath AVKit's interaction layer.
Together the controls provide:

- play and pause;
- 10-second backward and 30-second forward seeks;
- a scrub bar with elapsed and total time, described below;
- audio-track selection;
- subtitle selection, including Off;
- eye-order swapping for side-by-side and over-under playback;
- retry and locate recovery when Files-backed preparation fails; and
- Done, which persists progress and releases the session.

The MV-HEVC ornament remains fully visible while playback is paused, loading,
failed, or being scrubbed, while a pointer hovers over it, and while its audio
or subtitle list is open. Automatic fading is disabled while VoiceOver or Switch
Control is active. During uninterrupted playback the ornament fades to nearly
invisible three seconds after the last of those ends; it is never removed. The
faded controls stay in place and interactive, and a system hover effect brings
them back to full opacity while the wearer looks at them, so a look and pinch
works without a separate show-controls button. The reveal waits for a short
dwell, about a third of a second, so a passing glance does not pop the controls
up, and then fades in gently; after the wearer looks away the controls hold for about 1.5 seconds and then fade more
slowly. The timings live in `PlaybackHUDPresentation`. visionOS renders that effect out
of process and never reports where the wearer looks, so the app's own state
stays faded until an interaction or pinching the video surface reveals the
controls again.

The MV-HEVC scrub bar spans the movie's full length from the first frame and
shows the range that can be played right now as a lighter band: the player's
buffered range around the playhead.

The MV-HEVC audio language and subtitle lists open at the bottom of the window,
just in front of the video and directly above the controls, so they read as part of
the controls panel. Both buttons are enabled once the movie is ready. A list with
nothing to choose between says so: **This title has one audio track.** or **This
title has no subtitles.** The list is a RealityView attachment placed in the
video's own space. The alternatives each failed: growing the controls ornament
pushed the list below the window; a second ornament, or a SwiftUI view in the
window, sat behind the window's surface and was hidden; a plain SwiftUI overlay
floated at the front of the window's depth and loomed over the controls; and a
system menu presented from the ornament opened behind the video. The list is also
outside the controls' hover group, so only the row the wearer looks at highlights.
It shows up to five rows and scrolls beyond that, starting at the current
selection, so a disc with a dozen or more tracks stays usable. While a list is
open, the video surface does not take pinches, so they reach the list.

Packed-stereo controls use AVKit's native visibility behavior; revealing the
playback controls also reveals actions labeled **Eye Order: Normal** or
**Eye Order: Reversed** and **Done**.

AVKit owns the packed-stereo audio menu and may present multiple same-language
tracks with the same localized label. The app explicitly asserts the active
packed-stereo audio option after the item becomes ready, but it does not replace
or claim to relabel AVKit's native menu. The app-owned MV-HEVC audio list leaves
unique names unchanged and adds role and stable track-number details only when
labels collide.

The app saves in-progress playback positions and restores them when the same
library item is opened again. An inactive scene keeps playing: on visionOS that
phase covers Control Center, looking at another window, and other ordinary focus
changes, so the player only saves current progress then. Moving to the background
cancels pending autoplay and pauses playback, saving current progress when a
player item is available. Initial preparation and eye-order replacement remain
paused if they complete while the app is in the background. Returning active
restores scene permission but never infers a new play request; playback remains
paused until the user starts it. Loading or failed preparations never overwrite
an existing resume point because resume writes begin only after a player item
has reached ready state. While playing, the position is also saved every 10
seconds, so a forced quit or a closed window that runs no end path loses only
seconds. Completed or near-completed playback is cleared instead of resuming at
the end.

A shared movie's position is keyed by the Mac's `resumeKey`: a digest of the
file's name, size, and modification time. Unlike the movie's `id` (which digests
the folder approval's ID and path) and its `revision` (which includes change time
and generation), it survives removing and re-adding the folder, a Mac app rebuild,
and metadata churn on a network share; editing or replacing the file changes it.
A position saved under the older `shared:<id>:<revision>` key moves to the new key
the first time the movie is opened while that key still matches. A Mac that does
not send `resumeKey` keeps the older key.

Packed stereo playback uses a visionOS 26 custom video composition with one
stable two-buffer output contract: output zero is always tagged as the left eye
and output one as the right eye. The composition supplies and attaches one
rectangular `AVSpatialVideoConfiguration` to both outputs. Source spatial
metadata is retained when present; the synthetic checks use their known 90°
field of view and 64 mm baseline, while imported packed media is not assigned
invented camera geometry. A custom composition instruction carries the packed
layout and requested eye order, and the compositor swaps source regions into
those fixed semantic outputs. Rendering copies native biplanar YUV planes and
uses an explicit SDR BT.709 output contract instead of mis-tagging converted HDR
frames or converting through device RGB. Eye-order changes replace only the
immutable `AVPlayerItem` while retaining
the `AVPlayer`, presentation surface, source lease, playback time and intent, audio
selection, and subtitle selection. Composition-backed seeks wait for the newly
rendered frame before restoration completes.

The Library always exposes a **Built-in stereo checks** panel with bundled,
reproducibly generated HEVC fixtures for side-by-side and over-under playback.
No import or filename preparation is required. Each eye image carries an
exclusive `LEFT EYE ONLY` or `RIGHT EYE ONLY` label, an instruction to close the
other eye, and asymmetric depth markers. With **Eye Order: Normal**, blue should
appear behind the screen, green on the screen plane, and red in front. In SBS,
the left half is the left eye; in over-under, the top half is the left eye.
Close the right eye to see only `LEFT EYE ONLY`, then close the left eye to see
only `RIGHT EYE ONLY`. Reversed eye order should invert the depth and swap those
labels; return to Normal afterward. The depth-marker disparities are checked from decoded
bundled pixels, but the corrected fixtures still need hands-on Vision Pro
confirmation. The checks are silent; use the audio validation matrix for sound.
The app copies the fixtures into
Application Support using a versioned, size-checked installation and refreshes
their stable library records and bookmarks on every launch so app updates cannot
leave stale bundle-path references. During a check, AVKit exposes Eye Order as a
native playback action labeled Normal or Reversed.

Files-provider sources may be backed by SMB or other network storage, but the app
does not implement direct `smb://` transport, offline pinning,
or provider-specific progress percentages.

### Lost and slow share connections

A movie outside the app's own storage, such as one on an SMB share, plays through
`ShareConnectionMonitor` (#1131). An MV-HEVC or packed-stereo file is read by
`ShareFileResourceLoader`, an `AVAssetResourceLoader` delegate, rather than as a
plain file URL: as a plain file, `AVPlayer` kept its clock running through a stalled
share read and then jumped forward to catch up. Through the loader it buffers 20
seconds ahead, stops its clock when the buffer runs out, and continues from the
same frame when reads come back. A Blu-ray 3D title already stops its clock when its
read-ahead buffer runs dry.

A failed read on a share is tried again from the same place every half second
instead of ending playback, for files, MKVs and disc images alike; the disc reader
puts libbluray's file back where the read started before each retry. Only errors a
reconnect can cure are retried: lost permission or a missing file fails at once. A
read that keeps failing for a minute while the headset has a network gives up with
**The movie's share stopped answering, so playback stopped.**; time with no network
at all does not count. A request the player cancels, after a seek for example,
stops retrying, and leaving the player stops every retry, including those of an MKV
that is still opening. A read that has
gone unanswered for two seconds, or no network at all while the movie's volume is
remote, counts as a lost connection. The controls then stay up and say **Connection
lost. Playing what has loaded while it reconnects.** while buffered video plays, or
**Connection lost. Playback continues from here when it reconnects.** once it has
stopped. A slow share that is still answering shows **Loading…** instead.

## Blu-ray 3D Sources

Native Blu-ray playback (#711) reads Blu-ray 3D sources on the headset itself, with
no Mac (#1110). It plays an unencrypted 3D ISO, a disc folder (the folder that holds
`BDMV`, or `BDMV` itself), or an MKV that keeps the MVC 3D track, such as a MakeMKV
rip, from a [Location](#locations) or the app's Documents folder. Listing a source
opens it, chooses its title and stores that title with the item; the
details view shows the title's length, audio and subtitles, and **Play** plays it on
the headset (see [Native Blu-ray Video](#native-blu-ray-video)). Encrypted discs still need the Mac and the user's
own MakeMKV.

The code is in `macos/BDToAVPPlayer/BluraySource`:

- **Disc images and folders** open through libbluray. A title must be at least 60
  seconds, have one angle, and have the SSIF stereo file of every clip it plays. Each
  clip must start where the clip does or at an entry point, and end where it does or at
  an entry point, so the reader can join it to the next. Playable titles come first, then
  the disc's main feature, then the longest, then the lower playlist number. The main
  feature comes before length because a disc can hold a longer playlist of looping
  extras: Ocean Dreams has a 3.6-hour slideshow beside its 94-minute feature. The first
  playable title is the one the player uses.
- **The SSIF demuxer** (`bd3d_source.c`, shared with the Mac converter's `bd3d_reader`) pairs each
  base-view access unit (PID 0x1011) with the dependent view (0x1012) that has the
  same DTS. It delivers whole PES packets for the chosen audio and PG subtitle PIDs.
  `TransportStreamPESReader.parse` builds the `BluRayPESPacket` values, so every
  source reads PES headers the same way.
- **Matroska** is read by a small EBML parser. The video track must be
  `V_MPEG4/ISO/AVC` with an `mvcC` block addition mapping. Each block already holds a
  frame's base and dependent NAL units, so the reader only converts them to Annex B
  and puts the `mvcC` parameter sets (SPS, subset SPS and both PPS) before the first
  frame. Audio and subtitle blocks become `BluRayPESPacket` values with stream ID
  0xBD and the block's PTS. Matroska TrueHD has no AC-3 core and Matroska PCM has no
  Blu-ray LPCM header, so neither is offered to the audio player.

Every source delivers, in stream order, to a `Bluray3DPacketSink`:
`MVCAccessUnit` values (one stereo frame as Annex B for edge264, with its 33-bit PTS
and DTS) and `BluRayPESPacket` values for audio and subtitles. `Bluray3DPacketRouter`
sends audio to `HeadsetAudioPlayer`. Returning false from the sink stops the reader,
and the sink paces it by blocking. All timestamps are raw 33-bit 90 kHz values;
`BluRayPresentationTimeline` maps them to playback time with the title's first video
PTS as zero.

A title plays its clips in playlist order (#1142), up to the playlist's first
non-seamless connection. That is where MakeMKV ends a title too: Ocean Dreams and Fast X
each append a short clip that way, a 12-second logo and a second of black, which MakeMKV
lists as separate titles too short to keep, so both features end with their first clip. The reader rewrites every timestamp,
including those in audio and subtitle PES headers, onto the playlist's own timeline,
which starts at zero, so audio and video keep one clock across a clip boundary and
`discontinuity()` stays reserved for seeks. Each clip is read as a segment: its video
starts at the first random access unit of the part the playlist plays, views whose pair
was read before that are dropped, and audio and subtitles outside that part are dropped.
A random access unit is an IDR or an I picture; many discs, Ocean Dreams among them, have
a single IDR and an I picture at every entry point. When a playlist item starts inside
its clip, as Fast X's feature starts a second into its clip, the reader finds the entry
point in the clip information (CLPI) file's EP map and starts at the SSIF extent pair
that holds it. Base and dependent extents sit together in the SSIF, so that pair starts
after every earlier extent of both views; the dependent clip comes from the playlist's
stereo sub-path. The same mapping starts a read anywhere in a title, for seeking and
resuming (#1113).

The tests read fixtures cut from the Ocean Dreams and Fast X discs at their clip
boundaries, read through libudfread and libbluray (disc images are never mounted).
`BD_TO_AVP_OCEAN_DREAMS_ISO` and `BD_TO_AVP_FAST_X_ISO`, passed to the simulator with
the `TEST_RUNNER_` prefix, also read the real discs across their boundaries.

libbluray 1.4.1 and libudfread 1.2.0, both LGPL-2.1-or-later, are linked as separate
dynamic frameworks in `macos/Vendor/libbluray.xcframework` and
`macos/Vendor/libudfread.xcframework`. Each framework carries its `COPYING` text. They
are built from the same pinned source archives as the Mac's `bd3d_reader`, recorded in
`vendor/bluray-reader-macos-arm64.toml` and kept in `bd_to_avp/resources/notices/bluray-reader`.
Rebuild or verify them with:

```sh
uv run python -m scripts.build_bluray_visionos
uv run python -m scripts.build_bluray_visionos --verify-only
```

A qualification build (`BD_TO_AVP_QUALIFICATION`) measures read speed when launched
with `BD_TO_AVP_SOURCE_READ_ITEM` set to a library item's file name. It reads the
title as fast as the source allows for `BD_TO_AVP_SOURCE_READ_SECONDS` (default 120),
prints `SOURCE_READ` lines, and writes `source-read-<file name>.json` to Documents.

## Native Blu-ray Video

Shiny 3D Cinema decodes Blu-ray 3D video (H.264 MVC) on the headset with
[edge264-mvc](https://github.com/jens-duttke/edge264-mvc), BSD-3-Clause, at the
revision the Mac app pins in `bd_to_avp/resources/notices/edge264-mvc-build.json`.
It is linked as the separate dynamic framework `macos/Vendor/Edge264MVC.xcframework`,
which also holds the small wrapper in `native/edge264_mvc` and exports only the
wrapper's `bdavp_mvc_*` functions. The license text ships inside the framework and
in `macos/Vendor/Edge264MVC-notices`, next to the build's provenance. Rebuild it
from the pin, byte for byte, with:

```sh
uv run python scripts/build_edge264_visionos.py
```

`MVCDecoder` feeds each `MVCAccessUnit` to the decoder with its timeline ticks, and
frames come back in display order carrying those ticks. `StereoSampleBufferFactory`
copies each view into an NV12 pixel buffer, tags them `.stereoView(.leftEye)` and
`.stereoView(.rightEye)` (the base view is the left eye unless the title says
otherwise), and wraps both in one sample buffer for `AVSampleBufferVideoRenderer`,
the display path proven in #1103.

`NativeBlurayPlayback` plays a title with three threads. The reader fills a
`ReadAheadBuffer` up to 15 seconds ahead of the decoder (256 MB at most), because
SMB reads of the Rainforest ISO averaged 11.9 MB/s but had a slowest second of
0.81 MB/s against about 5 MB/s needed (#1110). The video thread decodes up to half
a second ahead of the clock and enqueues to `AVSampleBufferVideoRenderer`; the
renderer takes frames only once RealityKit shows it, so decoding waits for the
player view. The audio thread hands `HeadsetAudioPlayer` its packets 2.5 seconds
ahead of the clock. Both renderers join one `AVSampleBufferRenderSynchronizer`, so
pause, buffering and seeking move audio and video together.

The clock starts once a few frames are queued and two seconds are buffered. If the
renderer is about to run dry before the title ends, the clock stops, the player
shows that it is loading, and playback continues once four seconds are buffered
again. The buffer keeps 20 seconds behind the decoder, so a seek inside that
history or the loaded part ahead restarts at the nearest earlier random access unit
and shows
frames from the target on. Accepting a seek reserves that unit, so trimming history
cannot drop it before the video thread gets there. A seek anywhere else starts a new
read at the last random access unit before the target (#1113): the disc reader finds
it through the EP map (see [Blu-ray 3D Sources](#blu-ray-3d-sources)) and the Matroska
reader through the file's cues, or, without cues, the last cluster before the target
that starts with a keyframe. The buffer drops the old read and the audio before the
target, the decoder shows frames from the target on, and the read it replaced stops at
its next packet. Each decoding pass is bound to its seek's read, so a scrub that restarts
the buffer while the decoder is still starting the previous seek's pass cannot let that
pass take the new read's first unit.
Every clock change (start, pause, buffering,
seek, stop) takes one lock from the state it depends on through the synchronizer
write, so a pause can never be undone by a rate computed just before it.

If the chosen audio cannot play, because its decoder cannot start, 20 packets in a
row fail to decode, or the audio renderer fails, `HeadsetAudioPlayer.failure` says so
in plain words and the engine reports it while the video plays on. A failed renderer
stops queuing audio (decoded audio waiting for it is capped at 1,024 chunks anyway),
and a seek or another track tries again.

In the player, `MVHEVCPlayerSession` routes a Blu-ray 3D item to
`NativeBlurayPlayback` instead of `AVPlayer`, and the existing controls, look to
reveal and track lists drive it. The scrub bar spans the title's length, and its
lighter band is the loaded part. While the title loads more, the controls stay up
with a **Loading…** line. The audio list names what plays, for example
**English (DTS core)**, and switches without stopping. If the chosen audio cannot play, the controls
say so while the video plays on. The subtitle list offers
only Off and says the title's subtitles come later (#1114). A title resumes where the
wearer stopped, keyed by its library or Location item, across relaunches: playback
starts there the same way a seek outside the loaded part does.

The video is a RealityKit `VideoPlayerComponent(videoRenderer:)` sized inside the
window, and the controls are an ornament below the window. Until RealityKit reports
the video's size, the video is kept tiny, so it can never draw at its natural size
over the controls, the layout bug #1103 hit.

A qualification build (`BD_TO_AVP_QUALIFICATION`) plays a library item on its own
when launched with `BD_TO_AVP_NATIVE_PLAY_ITEM` set to its file name, for
`BD_TO_AVP_NATIVE_PLAY_SECONDS` (default 660). It pauses at
`BD_TO_AVP_NATIVE_PAUSE_AT` (120) for `BD_TO_AVP_NATIVE_PAUSE_SECONDS` (10),
switches audio at `BD_TO_AVP_NATIVE_AUDIO_SWITCH_AT` (240) and seeks 10 seconds
back at `BD_TO_AVP_NATIVE_SEEK_BACK_AT` (300). `BD_TO_AVP_NATIVE_SEEKS` adds seeks as
`second:target` pairs, such as `10:1200,30:5620`, and records how long each took to
play again at its target. It prints `NATIVE_PLAY` lines and
writes `native-playback-<file name>.json` to Documents, with position, buffering,
frames, CPU, thermal state and memory each second, the renderer's dropped-frame
count, and the window and video sizes.

## Native Blu-ray Audio

Native Blu-ray playback (#711) decodes the disc's own audio on the headset. The
components live in `macos/BDToAVPPlayer/NativeAudio`, and native playback feeds them
through `NativeBlurayPlayback`. No licensing fees are paid, so
lossless Dolby and DTS layers are not decoded
([decision](https://github.com/cbusillo/BD_to_AVP/issues/1112#issuecomment-6391365574)).
The audio list names what actually plays, for example **English (DTS core)**.

| Disc format | What plays | Decoder | List label |
|---|---|---|---|
| LPCM | All channels | Built in | PCM |
| Dolby Digital (AC-3) | All channels | Apple AudioToolbox | Dolby Digital |
| Dolby Digital Plus (E-AC-3) | Independent and dependent substreams, up to 7.1 | Apple AudioToolbox | Dolby Digital Plus |
| Dolby TrueHD | The AC-3 core every Blu-ray TrueHD track carries; the lossless layer is skipped | Apple AudioToolbox | Dolby Digital core |
| DTS | The DTS core | FFmpeg `dca` | DTS |
| DTS-HD High Resolution, DTS-HD Master Audio | The DTS core; the extension substream is skipped | FFmpeg `dca` with `core_only` | DTS core |
| DTS-HD secondary (DTS Express) | Not listed: it has no core | None | None |

FFmpeg is linked as `macos/Vendor/FFmpegDCA.xcframework`, a dynamic framework
holding FFmpeg 9.0.2's `libavcodec` and `libavutil` with only the `dca` decoder,
plus the wrapper in `native/ffmpeg_dca`. It is built without `--enable-gpl`,
`--enable-version3` or `--enable-nonfree`, so it is LGPL-2.1-or-later; the build
script checks FFmpeg's own license string, and its license text ships inside the
framework and in `macos/Vendor/FFmpegDCA-notices`. Rebuild it from the pinned
source in `vendor/ffmpeg-dca-visionos.toml` with:

```sh
uv run python scripts/build_ffmpeg_dca_visionos.py
```

The source reader hands audio to `HeadsetAudioPlayer` as complete PES packets
(`BluRayPESPacket`: PID, stream ID, `stream_id_extension`, 33-bit PTS, payload),
through `BluRayPESPacketSink`, with the title's audio streams described by
`BluRayAudioStreamDescriptor` (PID, coding type and language from the playlist).
`TransportStreamPESReader` builds those packets from M2TS bytes. On Blu-ray the
DTS core and the AC-3 part of E-AC-3 use PES extension 0x71, DTS-HD and E-AC-3
extension data use 0x72, and a TrueHD track's AC-3 core uses 0x76.

`HeadsetAudioPlayer` decodes the selected track to 32-bit float PCM and enqueues
it to an `AVSampleBufferAudioRenderer` that it adds to the caller's
`AVSampleBufferRenderSynchronizer`; the native video renderer joins the same
synchronizer. `BluRayPresentationTimeline` maps PTS to synchronizer time with the
title's first video PTS as zero, unwrapping the 33-bit counter, and both
renderers use it. AudioToolbox trims its decoder delay from its first output, so
Dolby output is timed by its own sample count rather than by each input frame.
The player keeps about a second of every other audio track behind the playhead.
Selecting another track flushes the renderer from about 100 ms ahead and
continues with the new track on the next frame boundary, without stopping the
clock. `wantsMoreData` tells the reader when about two seconds are queued.

## Build And Test

Generate the Xcode project from the checked-in specification:

```sh
uv run python scripts/native_app.py generate
```

Run the visionOS simulator unit suite when a compatible runtime and Apple Vision
Pro simulator device type are installed:

```sh
xcodebuild test \
  -project macos/BluRayToVisionPro.xcodeproj \
  -scheme BDToAVPPlayer \
  -configuration Debug \
  -destination 'platform=visionOS Simulator,name=Apple Vision Pro' \
  -derivedDataPath macos/build/BDToAVPPlayerDerivedData \
  CODE_SIGNING_ALLOWED=NO
```

The same scheme also contains `BDToAVPPlayerUITests`. Its built-in check test is
unconditional: it verifies that the stereo-check panel is visible from launch,
starts the SBS check, and confirms that AVKit exposes native Eye Order and Done
actions. XRSimulator does not dispatch those actions while it displays the
tagged-stereo unsupported-content placeholder, so the unit suite separately
drives the production eye-order rebuild from ready through reversed and ready
again.
Its seeded-media flow is skipped when `PlayerLongFixture.mov` is absent from the
simulator app Documents directory; when present, it verifies direct Library →
Play → Library, Library → Details → Play, Details Done → Library, player Done →
Details, replay, and ornament auto-hide behavior.

CI pins the release Xcode, regenerates the project, and always runs
`build-for-testing` against the generic visionOS Simulator destination. When an
available visionOS 26-or-newer runtime and Apple Vision Pro device type exist,
CI creates, boots, tests, and deletes a temporary simulator with
`test-without-building`.
If either prerequisite is absent, CI logs a clear successful skip instead of
assuming a pre-existing simulator.

For a connected headset, build with automatic development provisioning:

```sh
xcodebuild build \
  -project macos/BluRayToVisionPro.xcodeproj \
  -scheme BDToAVPPlayer \
  -configuration Debug \
  -destination 'platform=visionOS,id=<device-id>' \
  -derivedDataPath macos/build/BDToAVPPlayerDeviceDerivedData \
  -allowProvisioningUpdates
```

Simulator tests and screenshots do not prove stereoscopic depth, eye order,
comfort, long-session thermals, or headset-visible interaction. The accepted
physical layout keeps the RealityKit video surface at the playback probe's
conservative scale and depth offset so foreground stereo content remains behind
the native ornament; changes to that geometry still require physical Vision Pro
validation. Nothing is drawn behind the video, so where the window's shape and
the video's differ the room shows through instead of a black frame (#1132). The visionOS Simulator can expose the AVKit surface and contextual
actions but displays an unsupported-content placeholder for tagged stereo
output. The automated suite therefore also prepares each bundled fixture through
the real `AVPlayer` composition path and exercises the production rendering core
directly. It asserts normal and reversed eye pixels, fixed semantic eye tags,
native pixel format, chroma routing, color attachment propagation, fixture
installation, format detection, and UI discoverability. Physical Vision Pro
remains the only way to prove that AVKit routes those tagged outputs to the
intended eyes; that qualification is a single bounded gate after local tests
pass.
