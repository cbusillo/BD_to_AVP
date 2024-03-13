#!/bin/bash
#/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/cbusillo/BD_to_AVP/master/installer.sh)"

handle_error() {
    echo "Error: $1"
    exit 1
}

echo "Checking for Homebrew..."
BREW_PATH=""
if [[ -d "/opt/homebrew/bin" ]]; then
    # Apple Silicon
    BREW_PATH="/opt/homebrew/bin"
elif [[ -d "/usr/local/bin/brew" ]]; then
    # Intel Mac
    BREW_PATH="/usr/local/bin"
fi

if [ -z "$BREW_PATH" ]; then
    echo "Homebrew not found. Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || handle_error "Failed to install Homebrew"
    if [[ -d "/opt/homebrew/bin" ]]; then
        BREW_PATH="/opt/homebrew/bin"
    else
        BREW_PATH="/usr/local/bin"
    fi
    echo 'export PATH="'$BREW_PATH':$PATH"' >> ~/.zshrc
else
    echo "Homebrew is already installed."
fi

export PATH="$BREW_PATH:$PATH"

echo "Updating Homebrew..."
"$BREW_PATH/brew" update

echo "Installing dependencies..."
"$BREW_PATH/brew" install python@3.12 ffmpeg makemkv mp4box || handle_error "Failed to install dependencies"
"$BREW_PATH/brew" install --cask --no-quarantine wine-stable || handle_error "Failed to install wine-stable"

# Optionally handle spatial-media-kit-tool setup here if automated download and installation are feasible

echo "Installing Poetry..."
"$BREW_PATH/brew" install poetry || handle_error "Failed to install Poetry"

echo "Cloning BD_to_AVP repository..."
git clone https://github.com/cbusillo/BD_to_AVP.git || handle_error "Failed to clone BD_to_AVP repository"
cd BD_to_AVP || handle_error "Failed to change directory to BD_to_AVP"

echo "Setting up BD_to_AVP environment..."
poetry install || handle_error "Failed to set up BD_to_AVP environment with Poetry"

echo "BD_to_AVP environment setup complete."
echo "You can now run BD_to_AVP with the following command:"
echo "poetry run bd-to-avp"
