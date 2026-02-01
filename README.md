# Telegram bot for PDF files

It supports PDF files upload and the following commands:
- /list - list all current files,
- /merge - merge all current files,
- /extract - extract pages from a PDF,
- /images - convert PDF into images,
- /clear - clear the current list.

Bot environment variables:
- `BOT_TOKEN` - Telegram bot API token
- `ALLOWED_USERS` - comma-separated list of Telegram IDs of users allowed to use the bot
- `PDF_BOT_BASE_DIR` - directory to store user PDF files
- `MAX_FILE_MB` - limit max PDF size
- `SESSION_TTL` - time in seconds that user session is kept alive (i.e. while it stores user files)

Currently bot only works in whitelist mode. It means you must explicitly specify telegram IDs of users allowed to use the bot.

## Running bot

```bash
sudo apt update
sudo apt install -y python3 python3-venv \
  poppler-utils ghostscript qpdf

mkdir -p /opt/tg-pdf-bot
cd /opt/tg-pdf-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install aiogram aiofiles pydantic

sudo cp tg-pdf-bot.service /etc/systemd/system

sudo systemctl daemon-reload
sudo systemctl enable --now tg-pdf-bot
sudo systemctl status tg-pdf-bot --no-pager
```
