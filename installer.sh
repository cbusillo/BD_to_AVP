
handle_error() {
    echo "Error: $1"
    exit 1
}

echo "Checking for Homebrew..."
if ! command -v brew &>/dev/null; then
    echo "Homebrew not found. Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || handle_error "Failed to install Homebrew"
else
    echo "Homebrew is already installed."
fi

echo "Updating Homebrew..."
brew update

echo "Installing dependencies..."
brew install python@3.12 ffmpeg makemkv mp4box git || handle_error "Failed to install dependencies"
brew install --cask --no-quarantine wine-stable || handle_error "Failed to install wine-stable"

# Optionally handle spatial-media-kit-tool setup here if automated download and installation are feasible

echo "Installing Poetry..."
brew install poetry || handle_error "Failed to install Poetry"

echo "Cloning BD_to_AVP repository..."
git clone https://github.com/cbusillo/BD_to_AVP.git || handle_error "Failed to clone BD_to_AVP repository"
cd BD_to_AVP || handle_error "Failed to change directory to BD_to_AVP"

echo "Setting up BD_to_AVP environment..."
poetry install || handle_error "Failed to set up BD_to_AVP environment with Poetry"

echo "BD_to_AVP environment setup complete."
echo "You can now run BD_to_AVP with the following command:"
echo "poetry run bd-to-avp"