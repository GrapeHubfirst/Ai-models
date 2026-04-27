# Wrote README.md
# 🤖 AI Chat Interface
A free, local AI chat interface that uses browser automation to connect to multiple AI models without requiring API keys.
## ✨ Features
- **Multiple AI Models** - Chat with GLM5, ChatGPT, Perplexity, and more
- **No API Keys Needed** - Uses browser automation to access free AI sites
- **Battle Mode** - Compare two AI models side-by-side
- **Local & Private** - Runs entirely on your machine
- **Claude Code Integration** - Edit files directly with Claude Code
## 🚀 Quick Start
### Option 1: Standalone HTML (easiest)
1. Open `ai_chat_interface.html` in your browser
2. Select a model and start chatting!
### Option 2: Enhanced App (more features)
1. Start the relay server:
```bash
cd dist
python run.py
```
2. Open `index.html` in your browser
3. Select models and start chatting!
## 📋 Available Commands
### In terminal:
```bash
python ai.py /glm5 "your question"
python ai.py /chatgpt "your question"
python ai.py /perplexity "your question"
```
### In the HTML interface:
- **GLM5** - Best for coding (chat.z.ai)
- **ChatGPT** - OpenAI's chatbot
- **Perplexity** - AI-powered search
- **Arena** - Random model battles
- **Pollinations** - Image generation
## 🛠️ How It Works
This project uses Playwright to automate browsers and interact with free AI services. No API keys required - it logs into the services just like you would manually.
### Requirements
- Python 3.8+
- Brave or Chrome browser
- Playwright: `pip install playwright && playwright install chromium`
## 📁 Files
- `ai_proxy.py` - Browser automation for each AI
- `ai.py` - Command-line interface
- `ai_chat_interface.html` - Simple standalone UI
- `dist/` - Enhanced React app
## ⚠️ Notes
- Some AIs may show CAPTCHA on first run - solve it manually and it should remember
- First run may be slower as it loads browser
- Works best with Brave browser installed
## 🤝 Credits
Uses browser automation via Playwright to access free AI services.
