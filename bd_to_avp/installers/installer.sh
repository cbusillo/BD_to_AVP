#!/bin/zsh
#/bin/zsh -c "$(curl -fsSL https://raw.githubusercontent.com/cbusillo/BD_to_AVP/release/bd_to_avp/installers/installer.sh)"

handle_error() {
    echo "Error: $1"
    exit 1
}

echo "Checking macOS version and architecture..."
ARCH=$(uname -m)
MACOS_VERSION=$(sw_vers -productVersion)
MACOS_MAJOR_VERSION=${MACOS_VERSION%%.*}

if [[ "$ARCH" != "arm64" ]]; then
    handle_error "This script is intended for use on Apple Silicon (M1+) Macs only."
fi

if [[ "$MACOS_MAJOR_VERSION" -lt 14 ]]; then
    handle_error "This script requires macOS Sonoma 14.0 or higher."
fi

cd ~ || handle_error "Failed to change directory to ~"

echo "Checking for Homebrew..."
if command -v /opt/homebrew/bin/brew &>/dev/null; then
    BREW_PATH="/opt/homebrew/bin"
else
    echo "Homebrew not found. Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || handle_error "Failed to install Homebrew"
    BREW_PATH="/opt/homebrew/bin"

fi

touch ~/.zshrc
if ! grep -q "$BREW_PATH" ~/.zshrc; then
    echo "export PATH=\"$BREW_PATH:\$PATH\"" >> ~/.zshrc
fi
export PATH="$BREW_PATH:$PATH"

echo "Homebrew installed at $BREW_PATH."

echo "Updating Homebrew..."
"$BREW_PATH/brew" update

check_app_or_install() {
    local app_name="$1"
    local cask_name="$2"
    local app_path="/Applications/$app_name.app"

    if [ -d "$app_path" ]; then
        echo "$app_name is already installed at $app_path."
    else
        echo "$app_name not found in /Applications. Attempting to install via Homebrew Cask..."
        "$BREW_PATH/brew" install --cask --no-quarantine "$cask_name" || handle_error "Failed to install $cask_name"
    fi
}

echo "Installing dependencies..."
"$BREW_PATH/brew" install python@3.12 ffmpeg mkvtoolnix tesseract finnvoor/tools/fx-upscale --no-quarantine 2>/dev/null || handle_error "Failed to install dependencies"

if [ -d "/Applications/MakeMKV.app" ]; then
  "$BREW_PATH/brew" uninstall "makemkv"
fi

check_app_or_install "MakeMKV" "makemkv"
check_app_or_install "Wine Stable" "wine-stable"
if [ ! -L "$BREW_PATH/wine" ]; then
    ln -s /Applications/Wine\ Stable.app/Contents/Resources/wine/bin/wine "$BREW_PATH/wine"
fi


check_mp4box_version() {
    local required_version="2.2.1"
    local info_plist="/Applications/GPAC.app/Contents/Info.plist"
    local installed_version=""

    if [ -f "$info_plist" ]; then
        installed_version=$(grep -A1 "<key>CFBundleShortVersionString</key>" "$info_plist" | grep -o '2\.2\.1')

        if [ "$installed_version" == "$required_version" ]; then
            echo "MP4Box $required_version is already installed."
            return 0
        else
            echo "MP4Box is installed, but the version is not $required_version."
            return 1
        fi
    else
        echo "MP4Box is not installed."
        return 1
    fi
}

install_mp4box() {
    SCRIPT_DIR="$(dirname "$0")"
    local pkg_path="$SCRIPT_DIR/installers/gpac-2.2.1.pkg"

    if check_mp4box_version; then
        echo "Skipping MP4Box installation."
    else
        if [ -d "/Applications/GPAC.app" ]; then
            echo "Removing existing MP4Box installation..."
            sudo rm -rf "/Applications/GPAC.app"
        fi

        echo "Installing MP4Box 2.2.1..."
        sudo installer -pkg "$pkg_path" -target / || handle_error "Failed to install MP4Box"
    fi
}

install_mp4box

echo "Making BD_to_AVP executable accessible system-wide..."
#TODO: add terminal version


echo "Installing Rosetta 2 (if required)..."
if arch -x86_64 true 2>/dev/null; then
    echo "Rosetta 2 support detected."
else
    echo "Rosetta 2 not detected, attempting installation."
    /usr/sbin/softwareupdate --install-rosetta --agree-to-license
fi


$BREW_PATH/wineboot &> /dev/null;
