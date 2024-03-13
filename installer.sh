#!/bin/bash
#/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/cbusillo/BD_to_AVP/master/installer.sh)"

handle_error() {
    echo "Error: $1"
    exit 1
}

get_brew_path() {
    if command -v /usr/local/bin/brew &> /dev/null; then
        echo "/usr/local/bin/brew"
    elif command -v /opt/homebrew/bin/brew &> /dev/null; then
        echo "/opt/homebrew/bin/brew"
    else
        echo ""
    fi
}

update_zshrc() {
    ZSHRC_PATH="$HOME/.zshrc"
    if ! grep -q "export PATH=\"$1:\$PATH\"" "$ZSHRC_PATH"; then
        echo "export PATH=\"$1:\$PATH\"" >> "$ZSHRC_PATH"
        echo "Updated .zshrc with new PATH"
    else
        echo ".zshrc already contains the PATH"
    fi
}

BREW_PATH=$(get_brew_path)
if [ -z "$BREW_PATH" ]; then
    echo "Homebrew not found. Installing..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || handle_error "Failed to install Homebrew"
    BREW_PATH=$(get_brew_path)
fi

export PATH="$BREW_PATH:$PATH"
update_zshrc "$BREW_PATH"

echo "Installing Python 3.12..."
$BREW_PATH install python@3.12 || handle_error "Failed to install Python 3.12"

echo "Installing Poetry for dependency management..."
$BREW_PATH install poetry || handle_error "Failed to install Poetry"

echo "Installing required dependencies..."
$BREW_PATH install ffmpeg makemkv mp4box || handle_error "Failed to install ffmpeg, makemkv, or mp4box"
$BREW_PATH install --cask --no-quarantine wine-stable || handle_error "Failed to install wine-stable"

# Instructions for manual setup steps, like spatial-media-kit-tool, if needed
echo "Please follow the manual setup instructions provided in the README for any additional dependencies."

# Use Poetry to set up the BD_to_AVP project. Adjust based on your actual project setup.
echo "Setting up BD_to_AVP environment..."
poetry install || handle_error "Failed to set up BD_to_AVP environment with Poetry"

source "$HOME/.zshrc"
echo "BD_to_AVP environment setup complete."
