# Installation Guide

## Prerequisites

### Required

- **Windows 10 or 11**
- **Python 3.10+** from [python.org](https://python.org)
  - During installation, **check "Add Python to PATH"**
- **MTGA** installed and launched at least once
  - In MTGA Settings, enable **"Detailed Logs (Plugin Support)"**

### AI Backend (pick one)

| Mode | Setup | Cost |
|------|-------|------|
| **Online** (recommended) | Subscribe at [mtgacoach.com/subscribe](https://mtgacoach.com/subscribe) | Subscription |
| **Local** (free) | Install [Ollama](https://ollama.com), pull a model | Free |

---

## Step-by-Step Install

### 1. Install Python

Download from [python.org](https://python.org/downloads/).

When the installer opens:
- Check **"Add Python to PATH"** (bottom of first screen)
- Click "Install Now"

### 2. Extract the zip

Unzip to any folder, e.g. `C:\mtgacoach`.

### 3. Run the installer

Double-click **`install.bat`**.

The setup wizard will:
1. Check your Python version
2. Create a virtual environment (`venv/`)
3. Install all Python packages
4. Language selection for voice input/output
5. Voice input mode (push-to-talk, voice activation, or disabled)
6. Save everything to `~/.arenamcp/settings.json`

### 4. Set up your AI backend

#### Option A: Online (mtgacoach.com subscription)

1. Visit [mtgacoach.com/subscribe](https://mtgacoach.com/subscribe)
2. Enter your email to get a license key
3. In the coach TUI, type: `/key YOUR_LICENSE_KEY`

#### Option B: Local (Ollama, free)

1. Install from [ollama.com](https://ollama.com)
2. Pull a model:
```bash
ollama pull llama3.2      # Fast, 2GB VRAM
ollama pull gemma3:12b    # Better quality, 8GB VRAM
```
3. In the coach TUI, type: `/local ollama`

### 5. Launch the coach

Double-click **`coach.bat`**.

Start an MTGA game and the coach will begin tracking automatically.

---

## TUI Commands

| Command | Description |
|---------|-------------|
| `/key LICENSE_KEY` | Set your subscription license key |
| `/subscribe` | Open subscription page in browser |
| `/online` | Switch to online mode |
| `/local` | Show local model configuration |
| `/local ollama` | Configure for Ollama (localhost:11434) |
| `/local lmstudio` | Configure for LM Studio (localhost:1234) |
| `/local URL` | Configure custom OpenAI-compatible endpoint |
| `/update` | Check for updates |
| `/bugreport` | Submit a bug report |

---

## Manual Install (without install.bat)

```bash
cd mtgacoach

# Create virtual environment
python -m venv venv
venv\Scripts\activate

# Install dependencies
pip install -e .

# Launch
python -m arenamcp.standalone
```
