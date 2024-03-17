#!/bin/zsh
#/bin/zsh -c "$(curl -fsSL https://raw.githubusercontent.com/cbusillo/BD_to_AVP/release/installer.sh)"

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
    echo "export PATH=\"$BREW_PATH:\$PATH\"" >> ~/.zshrc
    export PATH="$BREW_PATH:$PATH"
fi

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
"$BREW_PATH/brew" install python@3.12 ffmpeg mp4box mkvtoolnix --no-quarantine  || handle_error "Failed to install dependencies"

check_app_or_install "MakeMKV" "makemkv"
check_app_or_install "Wine Stable" "wine-stable"
ln -s /Applications/Wine\ Stable.app/Contents/Resources/wine/bin/wine "$BREW_PATH/wine"

echo "Installing BD_to_AVP from PyPI..."
python3 -m pip install bd_to_avp || handle_error "Failed to install BD_to_AVP from PyPI"

echo "Installing Rosetta 2 (if required)..."
if arch -x86_64 true 2>/dev/null; then
    echo "Rosetta 2 support detected."
else
    echo "Rosetta 2 not detected, attempting installation."
    /usr/sbin/softwareupdate --install-rosetta --agree-to-license
fi

$BREW_PATH/wineboot &> /dev/null;



echo "BD_to_AVP environment setup complete. Refresh your terminal's environment to use new paths."
echo "1. Refresh environment: source $HOME/.zshrc"
echo "2. Execute BD_to_AVP: bd-to-avp"
