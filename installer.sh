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

fi

touch ~/.zshrc
if ! grep -q "$BREW_PATH" ~/.zshrc; then
    echo "export PATH=\"$BREW_PATH:\$PATH\"" >> ~/.zshrc
fi
export PATH="$BREW_PATH:$PATH"

echo "Homebrew installed at $BREW_PATH."

echo "Updating Homebrew..."
"$BREW_PATH/brew" update


echo "Installing dependencies..."
"$BREW_PATH/brew" install python@3.12  2>/dev/null || handle_error "Failed to install dependencies"


echo "Creating a virtual environment for BD_to_AVP..."
VENV_PATH="$HOME/.bd_to_avp_venv"
python3.12 -m venv "$VENV_PATH" || handle_error "Failed to create a virtual environment"

echo "Activating the virtual environment..."
source "$VENV_PATH/bin/activate"

echo "Installing or updating BD_to_AVP from PyPI..."
pip install --upgrade bd_to_avp || echo "Failed to install/update BD_to_AVP from PyPI"

echo "Making BD_to_AVP executable accessible system-wide..."

EXECUTABLE_PATH="$VENV_PATH/bin/bd-to-avp"
SYSTEM_WIDE_PATH="/opt/homebrew/bin/bd-to-avp"

if [ -L "$SYSTEM_WIDE_PATH" ] || [ -e "$SYSTEM_WIDE_PATH" ]; then
    echo "Existing BD_to_AVP executable or symlink found. Removing..."
    rm -f "$SYSTEM_WIDE_PATH"
fi

ln -s "$EXECUTABLE_PATH" "$SYSTEM_WIDE_PATH" || handle_error "Failed to link BD_to_AVP executable system-wide"
echo "BD_to_AVP is now accessible system-wide as 'bd-to-avp'"

echo "Installing Rosetta 2 (if required)..."
if arch -x86_64 true 2>/dev/null; then
    echo "Rosetta 2 support detected."
else
    echo "Rosetta 2 not detected, attempting installation."
    /usr/sbin/softwareupdate --install-rosetta --agree-to-license
fi


