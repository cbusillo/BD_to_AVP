# BD-to-AVP Conversion Tool README

## Introduction

This tool processes 3D video content from Blu-ray discs, ISO images, or MKV files, creating a final video file compatible with
various viewing platforms. It uses FFmpeg, MakeMKV, and Wine for video extraction, audio transcoding, and video stream merging.

## Prerequisites

Ensure the following are installed on your Mac:

- **Homebrew**: The missing package manager for macOS (or Linux).
- **FFmpeg**: A complete, cross-platform solution to record, convert, and stream audio and video.
- **Wine**: A free and open-source compatibility layer allowing Windows programs to run on Unix-like operating systems.
- **MakeMKV**: For converting disc video content into MKV files.

### Installing Homebrew

Open Terminal and run:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

### Installing FFmpeg

With Homebrew installed:

```bash
brew install ffmpeg
```

### Installing Wine

Install Wine via Homebrew:

```bash
brew tap homebrew/cask-versions
brew install --cask --no-quarantine wine-stable
```

### Installing MakeMKV

Install MakeMKV via Homebrew:

```bash
brew install makemkv
```

### Installing spatial-media-kit-tool

Download spatial-media-kit-tool from the [releases](https://github.com/sturmen/SpatialMediaKit/releases) page

```bash
chmod +x spatial-media-kit-tool
sudo cp spatial-media-kit-tool /usr/local/bin
```

### Installing MP4Box

Install MP4Box via Homebrew:

```bash
brew install mp4box
```

## Usage

Navigate to the tool's directory in your terminal and execute the command with the required and optional parameters:

### Command Syntax

```bash
python main.py --source <source_path> [--output_folder <output_path>] [options]
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
