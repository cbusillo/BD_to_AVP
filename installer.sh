#!/bin/bash
#/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/cbusillo/BD_to_AVP/master/installer.sh)"

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
"$BREW_PATH/brew" install python@3.12 ffmpeg mp4box || handle_error "Failed to install dependencies"

check_app_or_install "MakeMKV" "makemkv"
check_app_or_install "Wine Stable" "wine-stable"
ln -s /Applications/Wine\ Stable.app/Contents/Resources/wine/bin/wine "$BREW_PATH/wine"


echo "Downloading spatial-media-kit-tool from the specific release..."
download_url="https://github.com/sturmen/SpatialMediaKit/releases/download/v0.0.8-alpha/spatial-media-kit-tool"
curl -L "$download_url" -o spatial-media-kit-tool || handle_error "Failed to download spatial-media-kit-tool"

chmod +x spatial-media-kit-tool
mv spatial-media-kit-tool "$BREW_PATH" || handle_error "Failed to move spatial-media-kit-tool to $BREW_PATH"
echo "spatial-media-kit-tool installed successfully."


echo "Installing Poetry..."
"$BREW_PATH/brew" install poetry || handle_error "Failed to install Poetry"
export PATH="$HOME/.poetry/bin:$PATH"

echo "Cloning BD_to_AVP repository..."
REPO_URL="https://github.com/cbusillo/BD_to_AVP.git"
CLONE_DIR="$HOME/BD_to_AVP"

cd ~ || handle_error "Failed to change directory to ~"
if [ -d "$CLONE_DIR" ]; then
    echo "$CLONE_DIR directory already exists. Checking for updates..."
    cd "$CLONE_DIR" || handle_error "Failed to change directory to $CLONE_DIR"
    git pull || handle_error "Failed to update $CLONE_DIR repository"
else
    echo "Cloning BD_to_AVP repository..."
    git clone "$REPO_URL" "$CLONE_DIR" || handle_error "Failed to clone BD_to_AVP repository"
    cd "$CLONE_DIR" || handle_error "Failed to change directory to $CLONE_DIR"
fi

echo "Setting up BD_to_AVP environment..."
poetry install || handle_error "Failed to set up BD_to_AVP environment with Poetry"

echo "Installing Rosetta 2 (if required)..."
if arch -x86_64 true 2>/dev/null; then
    echo "Rosetta 2 support detected."
else
    echo "Rosetta 2 not detected, attempting installation."
    /usr/sbin/softwareupdate --install-rosetta --agree-to-license
fi


echo "BD_to_AVP environment setup complete."
echo "Navigate to the BD_to_AVP directory and run BD_to_AVP with the following command:"
echo "cd $CLONE_DIR && poetry run bd-to-avp"