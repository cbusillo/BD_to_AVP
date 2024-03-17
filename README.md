# BD-to-AVP Blu-ray Disc to Apple Vision Pro README

## Introduction

This tool processes 3D video content from Blu-ray discs, ISO images, or MKV files, creating a final video file compatible with
the Apple Vision Pro. It uses FFmpeg, MakeMKV, and Wine for video extraction, audio transcoding, and video stream merging to convert
from Mpeg 4 MVC 3D video to MV-HEVC 3D video. The tool also injects 360° metadata into the video file for spatial media playback.

## Quick install

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/cbusillo/BD_to_AVP/master/installer.sh)"
```

## Prerequisites

Ensure the following are installed on your Mac:

- **Apple Silicon [Mac]**: A Mac with Apple Silicon, such as the M1, M1 Pro, or M1 Max (Maybe, installer currently requires Apple
  Silicon. If anyone can confirm that spatial-media-kit-tool works on Intel Macs, I can remove this requirement)
- **[macOS Sonoma]**: The latest version of macOS.
- **[Rosetta 2]**: A compatibility layer allowing Intel-based applications to run on Apple Silicon Macs.
- **[Python] 3.12**: The latest version of Python.
- **[Poetry]**: A dependency manager for Python.
- **[Homebrew]**: The missing package manager for macOS (or Linux).
- **[FFmpeg]**: A complete, cross-platform solution to record, convert, and stream audio and video.
- **[Wine]**: A free and open-source compatibility layer allowing Windows programs to run on Unix-like operating systems.
- **[MakeMKV]**: For converting disc video content into MKV files.
- **[spatial-media-kit-tool]**: A tool for injecting 360° metadata into video files.
- **[MP4Box]**: A multimedia packager available for Windows, Mac, and Linux.

## Manual Installation

To set up your macOS environment for video processing, including creating and handling 3D video content, follow these steps to
install the necessary tools using Homebrew and manual installation. This includes the installation of Homebrew itself, FFmpeg for
video encoding and decoding, Wine for running Windows applications, MakeMKV for ripping Blu-ray and DVD to MKV,
spatial-media-kit-tool for handling spatial media, and MP4Box for multimedia packaging.

```bash
# Install Rosetta 2
/usr/sbin/softwareupdate --install-rosetta --agree-to-license

# Install Homebrew
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install FFmpeg, MakeMKV, MP4Box, and Python 3.12
brew install ffmpeg makemkv mp4box python@3.12

# Install Wine
brew tap homebrew/cask-versions
brew install --cask --no-quarantine wine-stable

# Install Poetry
curl -sSL https://install.python-poetry.org | python3 -

# Ensure Python 3.12 and Poetry are correctly installed
python3.12 -m pip install --upgrade pip

cd /path/to/BD_to_AVP
poetry install
```

## Usage

Navigate to the tool's directory in your terminal and execute the command with the required and optional parameters:

### Command Syntax

```bash
poetry run bd-to-avp --source <source> --output_folder <output_folder> [--keep_intermediate] [--transcode_audio] [--audio_bitrate <audio_bitrate>] [--mv_hevc_quality <mv_hevc_quality>] [--fov <fov>] [--frame_rate <frame_rate>] [--resolution <resolution>]
```

### Parameters

- `--source`: Source disc number, MKV file path, or ISO image path (required).
- `--output_root_folder`: Output folder path. Defaults to the current directory.
- `--transcode_audio`: Enable audio transcoding to AAC (disabled by default).
- `--audio_bitrate`: Audio bitrate for transcoding in kb/s do not include unit (default: "384").
- `--left_right_bitrate`: Bitrate for left and right views in Mb/s do not include unit (default: "20").'
- `--mv_hevc_quality`: Quality factor for MV-HEVC encoding (default: "75").
- `--fov`: Horizontal field of view for MV-HEVC (default: "90").
- `--frame_rate`: Video frame rate (auto-detected if not provided).
- `--resolution`: Video resolution (auto-detected if not provided).
- `--keep_files`: Keep intermediate files (disabled by default).

### Examples

Process a Blu-ray disc:

```bash
poetry run bd-to-avp --source disc:0 --output_folder /path/to/output
```

Process an ISO image:

```bash
poetry run bd-to-avp --source /path/to/movie.iso --output_folder /path/to/output
```

Process an MKV file:

```bash
poetry run bd-to-avp --source /path/to/movie.mkv --output_folder /path/to/output --transcode_audio
```

## Contribution

Contribute to the project by submitting pull requests or opening issues for bugs and feature requests.

## Acknowledgements

Big thanks to:

- [sturmen][sturmen] on the Doom9 forums, for [an encoding guide][sturmen-guide] using `FRIM Decoder` as well as creating
  the [spatial-media-kit-tool]
- [Vargol][vargol] on GitHub, for making the [JM reference software][jm-reference] [build properly on macOS][vargol-tools] as well as
  an [example script][vargol-guide] that was a useful reference
- [steverice][steverice] for [h264-tools][ldecod]

[MakeMKV]: https://www.makemkv.com/

[FFmpeg]: https://ffmpeg.org/

[jm-reference]: https://iphome.hhi.de/suehring/

[ldecod]: https://github.com/steverice/h264-tools

[spatial-media-kit-tool]: https://github.com/sturmen/SpatialMediaKit

[MP4Box]: https://github.com/gpac/gpac/wiki/MP4Box

[sturmen]: https://forum.doom9.org/member.php?u=224594

[sturmen-guide]: https://forum.doom9.org/showthread.php?p=1996846#post1996846

[vargol]: https://github.com/Vargol

[vargol-tools]: https://github.com/Vargol/h264-tools

[vargol-guide]: https://github.com/Vargol/h264-tools/wiki/Conversion-script-for-MVC-3D-blu-ray-extracted-by--MakeMKV

[steverice]: https://github.com/steverice

[h264-tools]: https://github.com/steverice/h264-tools

[Wine]: https://www.winehq.org/

[Homebrew]: https://brew.sh/

[Poetry]: https://python-poetry.org/

[Python]: https://www.python.org/

[Mac]: https://www.apple.com/mac/

[macOS Sonoma]:https://apps.apple.com/us/app/macos-sonoma/id6450717509?mt=12

[Rosetta 2]: https://support.apple.com/en-us/HT211861