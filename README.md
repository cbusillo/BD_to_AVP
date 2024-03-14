# BD-to-AVP Conversion Tool README

## Introduction

This tool processes 3D video content from Blu-ray discs, ISO images, or MKV files, creating a final video file compatible with
various viewing platforms. It uses FFmpeg, MakeMKV, and Wine for video extraction, audio transcoding, and video stream merging.

## Quick install

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/cbusillo/BD_to_AVP/master/installer.sh)"
```

## Prerequisites

Ensure the following are installed on your Mac:

- **macOS Sonoma**: The latest version of macOS.
- **Rosetta 2**: A compatibility layer allowing Intel-based applications to run on Apple Silicon Macs.
- **Python 3.12**: The latest version of Python.
- **Poetry**: A dependency manager for Python.
- **Homebrew**: The missing package manager for macOS (or Linux).
- **FFmpeg**: A complete, cross-platform solution to record, convert, and stream audio and video.
- **Wine**: A free and open-source compatibility layer allowing Windows programs to run on Unix-like operating systems.
- **MakeMKV**: For converting disc video content into MKV files.
- **spatial-media-kit-tool**: A tool for injecting 360° metadata into video files.
- **MP4Box**: A multimedia packager available for Windows, Mac, and Linux.

## Installation

To set up your macOS environment for video processing, including creating and handling 3D video content, follow these steps to install the necessary tools using Homebrew and manual installation. This includes the installation of Homebrew itself, FFmpeg for video encoding and decoding, Wine for running Windows applications, MakeMKV for ripping Blu-ray and DVD to MKV, spatial-media-kit-tool for handling spatial media, and MP4Box for multimedia packaging.

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

# Install spatial-media-kit-tool
# Download spatial-media-kit-tool from https://github.com/sturmen/SpatialMediaKit/releases
chmod +x spatial-media-kit-tool
sudo cp spatial-media-kit-tool "$HOMEBREW_PREFIX/bin"

# Install Poetry
curl -sSL https://install.python-poetry.org | python3 -

# Ensure Python 3.12 and Poetry are correctly installed
python3.12 -m pip install --upgrade pip
```

## Usage

Navigate to the tool's directory in your terminal and execute the command with the required and optional parameters:

### Command Syntax

```bash
cd /path/to/BD_to_AVP
poetry install
poetry run bd_to_avp --source <source> --output_folder <output_folder> [--keep_intermediate] [--transcode_audio] [--audio_bitrate <audio_bitrate>] [--mv_hevc_quality <mv_hevc_quality>] [--fov <fov>] [--frame_rate <frame_rate>] [--resolution <resolution>]
```

### Parameters

- `--source`: Source disc number, MKV file path, or ISO image path (required).
- `--output_folder`: Output folder path. Defaults to the current directory.
- `--keep_intermediate`: Keep intermediate files (default: delete).
- `--transcode_audio`: Enable audio transcoding to AAC (disabled by default).
- `--audio_bitrate`: Audio bitrate for transcoding (default: "384k").
- `--mv_hevc_quality`: Quality factor for MV-HEVC encoding (default: "75").
- `--fov`: Horizontal field of view for MV-HEVC (default: "90").
- `--frame_rate`: Video frame rate (auto-detected if not provided).
- `--resolution`: Video resolution (auto-detected if not provided).

### Examples

Process a Blu-ray disc:

```bash
python main.py --source disc:0 --output_folder /path/to/output
```

Process an ISO image:

```bash
python main.py --source /path/to/movie.iso --output_folder /path/to/output
```

Process an MKV file:

```bash
python main.py --source /path/to/movie.mkv --output_folder /path/to/output --transcode_audio
```

## Contribution

Contribute to the project by submitting pull requests or opening issues for bugs and feature requests.
