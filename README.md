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

