import asyncio
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import List, Dict, Tuple, Optional

import aiofiles
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    FSInputFile,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InputMediaDocument,
)

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN env var")

ALLOWED_USERS = set()
allowed_env = os.environ.get("ALLOWED_USERS", "").strip()
if allowed_env:
    ALLOWED_USERS = {int(x.strip()) for x in allowed_env.split(",") if x.strip()}

BASE_DIR = Path(os.environ.get("PDF_BOT_BASE_DIR", "/opt/tg-pdf-bot/data")).resolve()
BASE_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "45"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
SESSION_TTL = int(os.environ.get("SESSION_TTL", str(6 * 3600)))

dp = Dispatcher()

# user_id -> ("action", step, payload)
# action: "extract" | "images" | "compress"
# step: 1 (need file index) or 2 (need param like range/dpi/preset)
PENDING: Dict[int, Tuple[str, int, dict]] = {}

# =========================
# UI
# =========================
BTN_LIST = "📄 List"
BTN_CLEAR = "🧹 Clear"
BTN_MERGE = "🧩 Merge all"
BTN_EXTRACT = "✂️ Extract pages"
BTN_IMAGES = "🖼 PDF → images"
BTN_COMPRESS = "🗜 Compress"
BTN_HELP = "ℹ️ Help"
BTN_CANCEL = "❌ Cancel"

def menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_LIST), KeyboardButton(text=BTN_HELP)],
            [KeyboardButton(text=BTN_MERGE), KeyboardButton(text=BTN_CLEAR)],
            [KeyboardButton(text=BTN_EXTRACT), KeyboardButton(text=BTN_IMAGES)],
            [KeyboardButton(text=BTN_COMPRESS), KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        selective=True,
    )

HELP_TEXT = """PDF bot

Workflow:
1) Send PDFs to chat.
2) Use menu buttons or commands.

Commands:
  /list
  /clear
  /merge
  /extract <file_index> <ranges>
  /images <file_index> [dpi]
  /compress <file_index> <preset>

Ranges examples:
  1-10
  2-
  2-4,5,6-10

Compression presets:
  screen | ebook | printer | prepress
"""

# =========================
# SECURITY / SESSION
# =========================
def check_allowed(message: Message) -> bool:
    if not ALLOWED_USERS:
        return False  # don't accidentally run public
    return message.from_user is not None and message.from_user.id in ALLOWED_USERS

def user_dir(user_id: int) -> Path:
    d = BASE_DIR / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d

def now_ts() -> int:
    return int(time.time())

def touch_session(d: Path) -> None:
    (d / ".last").write_text(str(now_ts()), encoding="utf-8")

def session_last(d: Path) -> int:
    p = d / ".last"
    if not p.exists():
        return 0
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except Exception:
        return 0

def cleanup_old_sessions() -> None:
    for d in BASE_DIR.iterdir():
        if not d.is_dir():
            continue
        last = session_last(d)
        if last and now_ts() - last > SESSION_TTL:
            shutil.rmtree(d, ignore_errors=True)

# =========================
# UTIL
# =========================
def run_cmd(cmd: List[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n\nSTDERR:\n{proc.stderr[-4000:]}"
        )

def safe_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name or "file.pdf"

def list_pdfs(d: Path) -> List[Path]:
    return sorted([p for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"])

def pdf_page_count(pdf: Path) -> int:
    proc = subprocess.run(["pdfinfo", str(pdf)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"pdfinfo failed:\n{proc.stderr[-2000:]}")
    m = re.search(r"^Pages:\s+(\d+)\s*$", proc.stdout, re.MULTILINE)
    if not m:
        raise RuntimeError("Could not read page count from pdfinfo output")
    return int(m.group(1))

def parse_ranges(spec: str, total_pages: int) -> List[int]:
    spec = spec.strip()
    if not spec:
        raise ValueError("Empty range spec")

    parts = [p.strip() for p in spec.split(",") if p.strip()]
    pages: List[int] = []
    seen = set()

    def add_page(n1: int):
        if 1 <= n1 <= total_pages:
            idx = n1 - 1
            if idx not in seen:
                seen.add(idx)
                pages.append(idx)

    for part in parts:
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = a.strip(), b.strip()
            if not a:
                raise ValueError(f"Bad range '{part}' (missing start)")
            start = int(a)
            end = total_pages if b == "" else int(b)
            if end < start:
                raise ValueError(f"Bad range '{part}' (end < start)")
            start = max(1, start)
            end = min(total_pages, end)
            for n in range(start, end + 1):
                add_page(n)
        else:
            add_page(int(part))

    if not pages:
        raise ValueError("No valid pages selected (maybe out of bounds?)")
    return pages

def pages_to_compact_ranges(pages_1_based_sorted: List[int]) -> str:
    if not pages_1_based_sorted:
        return ""
    ranges = []
    s = e = pages_1_based_sorted[0]
    for x in pages_1_based_sorted[1:]:
        if x == e + 1:
            e = x
        else:
            ranges.append(f"{s}-{e}" if s != e else f"{s}")
            s = e = x
    ranges.append(f"{s}-{e}" if s != e else f"{s}")
    return ",".join(ranges)

# =========================
# PDF OPS
# =========================
def merge_pdfs(inputs: List[Path], out: Path) -> None:
    cmd = ["qpdf", "--empty", "--pages"] + [str(p) for p in inputs] + ["--", str(out)]
    run_cmd(cmd)

def extract_pages(src: Path, pages_0_based: List[int], out: Path) -> None:
    page_nums = [str(i + 1) for i in pages_0_based]
    cmd = ["qpdf", str(src), "--pages", str(src)] + page_nums + ["--", str(out)]
    run_cmd(cmd)

def pdf_to_images(src: Path, out_dir: Path, dpi: int = 150) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / "page"
    cmd = ["pdftoppm", "-png", "-r", str(dpi), str(src), str(prefix)]
    run_cmd(cmd)
    imgs = sorted(out_dir.glob("page-*.png"))
    if not imgs:
        raise RuntimeError("No images produced")
    return imgs

def compress_pdf(src: Path, out: Path, preset: str) -> None:
    if preset not in {"screen", "ebook", "printer", "prepress"}:
        raise ValueError("preset must be one of: screen, ebook, printer, prepress")

    tmp = out.with_suffix(".tmp.pdf")
    cmd = [
        "gs",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        "-dNOPAUSE", "-dBATCH", "-dSAFER",
        f"-dPDFSETTINGS=/{preset}",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-dDownsampleColorImages=true",
        "-dDownsampleGrayImages=true",
        "-dDownsampleMonoImages=true",
        "-sOutputFile=" + str(tmp),
        str(src),
    ]
    run_cmd(cmd)
    run_cmd(["qpdf", "--linearize", str(tmp), str(out)])
    tmp.unlink(missing_ok=True)

# =========================
# COMMANDS
# =========================
@dp.message(Command("start"))
async def start(message: Message):
    if not check_allowed(message):
        return
    d = user_dir(message.from_user.id)
    touch_session(d)
    await message.answer(HELP_TEXT, reply_markup=menu_kb())

@dp.message(Command("help"))
async def help_cmd(message: Message):
    if not check_allowed(message):
        return
    d = user_dir(message.from_user.id)
    touch_session(d)
    await message.answer(HELP_TEXT, reply_markup=menu_kb())

@dp.message(Command("clear"))
async def clear_cmd(message: Message):
    if not check_allowed(message):
        return
    user_id = message.from_user.id
    d = user_dir(user_id)
    touch_session(d)
    PENDING.pop(user_id, None)

    for p in d.iterdir():
        if p.name == ".last":
            continue
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)

    await message.answer("Cleared your session files.", reply_markup=menu_kb())

@dp.message(Command("list"))
async def list_cmd(message: Message):
    if not check_allowed(message):
        return
    d = user_dir(message.from_user.id)
    touch_session(d)
    cleanup_old_sessions()

    pdfs = list_pdfs(d)
    if not pdfs:
        await message.answer("No PDFs uploaded yet.", reply_markup=menu_kb())
        return

    lines = ["Your PDFs:"]
    for i, p in enumerate(pdfs, start=1):
        size_mb = p.stat().st_size / (1024 * 1024)
        lines.append(f"{i}) {p.name} ({size_mb:.1f} MB)")
    await message.answer("\n".join(lines), reply_markup=menu_kb())

@dp.message(Command("merge"))
async def merge_cmd(message: Message):
    if not check_allowed(message):
        return
    d = user_dir(message.from_user.id)
    touch_session(d)
    cleanup_old_sessions()

    pdfs = list_pdfs(d)
    if len(pdfs) < 2:
        await message.answer("Upload at least 2 PDFs, then merge.", reply_markup=menu_kb())
        return

    out = d / "merged.pdf"
    try:
        merge_pdfs(pdfs, out)
    except Exception as e:
        await message.answer(f"Merge failed:\n{e}", reply_markup=menu_kb())
        return

    await message.answer_document(FSInputFile(out), caption="Merged PDF", reply_markup=menu_kb())

@dp.message(Command("extract"))
async def extract_cmd(message: Message):
    if not check_allowed(message):
        return
    d = user_dir(message.from_user.id)
    touch_session(d)
    cleanup_old_sessions()

    args = (message.text or "").split(maxsplit=2)
    if len(args) < 3:
        await message.answer("Usage: /extract <file_index> <ranges>", reply_markup=menu_kb())
        return

    idx = int(args[1])
    ranges = args[2].strip()

    pdfs = list_pdfs(d)
    if idx < 1 or idx > len(pdfs):
        await message.answer("Bad file_index. Use /list.", reply_markup=menu_kb())
        return

    src = pdfs[idx - 1]
    try:
        total = pdf_page_count(src)
        pages0 = parse_ranges(ranges, total)
        pages1 = sorted({p + 1 for p in pages0})
        compact = pages_to_compact_ranges(pages1)

        out = d / f"extract_{src.stem}_{safe_name(ranges)}.pdf"
        extract_pages(src, pages0, out)

        await message.answer(
            "Extract result:\n"
            f"- Source: {src.name}\n"
            f"- Total pages: {total}\n"
            f"- Requested: {ranges}\n"
            f"- Resolved pages ({len(pages1)}): {compact}\n"
            f"- Output: {out.name}",
            reply_markup=menu_kb(),
        )
        await message.answer_document(FSInputFile(out), caption="Extracted PDF", reply_markup=menu_kb())
    except Exception as e:
        await message.answer(f"Extract failed:\n{e}", reply_markup=menu_kb())

@dp.message(Command("images"))
async def images_cmd(message: Message):
    if not check_allowed(message):
        return
    d = user_dir(message.from_user.id)
    touch_session(d)
    cleanup_old_sessions()

    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer("Usage: /images <file_index> [dpi]", reply_markup=menu_kb())
        return

    idx = int(args[1])
    dpi = int(args[2]) if len(args) >= 3 else 150
    dpi = max(72, min(dpi, 400))

    pdfs = list_pdfs(d)
    if idx < 1 or idx > len(pdfs):
        await message.answer("Bad file_index. Use /list.", reply_markup=menu_kb())
        return

    src = pdfs[idx - 1]
    out_dir = d / f"images_{src.stem}_{dpi}dpi"
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)

    try:
        imgs = pdf_to_images(src, out_dir, dpi=dpi)
        n = len(imgs)

        if n <= 10:
            await message.answer(f"Sending {n} images as documents (no compression).", reply_markup=menu_kb())
            media = [InputMediaDocument(media=FSInputFile(p)) for p in imgs]
            await message.bot.send_media_group(chat_id=message.chat.id, media=media)
        else:
            zip_path = d / f"{out_dir.name}.zip"
            if zip_path.exists():
                zip_path.unlink(missing_ok=True)
            shutil.make_archive(str(zip_path.with_suffix("")), "zip", out_dir)
            await message.answer_document(
                FSInputFile(zip_path),
                caption=f"Images for {src.name} ({dpi} dpi) — {n} pages (zipped).",
                reply_markup=menu_kb(),
            )
    except Exception as e:
        await message.answer(f"Convert failed:\n{e}", reply_markup=menu_kb())

@dp.message(Command("compress"))
async def compress_cmd(message: Message):
    if not check_allowed(message):
        return
    d = user_dir(message.from_user.id)
    touch_session(d)
    cleanup_old_sessions()

    args = (message.text or "").split()
    if len(args) != 3:
        await message.answer("Usage: /compress <file_index> <preset>", reply_markup=menu_kb())
        return

    idx = int(args[1])
    preset = args[2].strip().lower()

    pdfs = list_pdfs(d)
    if idx < 1 or idx > len(pdfs):
        await message.answer("Bad file_index. Use /list.", reply_markup=menu_kb())
        return

    src = pdfs[idx - 1]
    out = d / f"compressed_{preset}_{src.name}"

    try:
        compress_pdf(src, out, preset=preset)
        before = src.stat().st_size
        after = out.stat().st_size
        ratio = (after / before) if before else 1.0
        await message.answer_document(
            FSInputFile(out),
            caption=f"Compressed ({preset}). Size: {before/1e6:.1f}MB → {after/1e6:.1f}MB ({ratio:.2f}x)",
            reply_markup=menu_kb(),
        )
    except Exception as e:
        await message.answer(f"Compress failed:\n{e}", reply_markup=menu_kb())

# =========================
# FILE UPLOAD
# =========================
@dp.message(F.document)
async def on_document(message: Message, bot: Bot):
    if not check_allowed(message):
        return

    doc = message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".pdf"):
        await message.answer("Send a PDF file (.pdf).", reply_markup=menu_kb())
        return

    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        await message.answer(f"File too large (> {MAX_FILE_MB} MB).", reply_markup=menu_kb())
        return

    d = user_dir(message.from_user.id)
    touch_session(d)

    fname = safe_name(doc.file_name)
    target = d / fname
    if target.exists():
        stem, suf = target.stem, target.suffix
        k = 2
        while True:
            cand = d / f"{stem}_{k}{suf}"
            if not cand.exists():
                target = cand
                break
            k += 1

    file = await bot.get_file(doc.file_id)
    data = await bot.download_file(file.file_path)

    async with aiofiles.open(target, "wb") as f:
        await f.write(data.read())

    await message.answer(f"Saved: {target.name}", reply_markup=menu_kb())

# =========================
# MENU BUTTONS (ReplyKeyboard)
# =========================
@dp.message(F.text == BTN_HELP)
async def btn_help(message: Message):
    if not check_allowed(message):
        return
    await message.answer(HELP_TEXT, reply_markup=menu_kb())

@dp.message(F.text == BTN_LIST)
async def btn_list(message: Message):
    await list_cmd(message)

@dp.message(F.text == BTN_CLEAR)
async def btn_clear(message: Message):
    await clear_cmd(message)

@dp.message(F.text == BTN_MERGE)
async def btn_merge(message: Message):
    await merge_cmd(message)

@dp.message(F.text == BTN_CANCEL)
async def btn_cancel(message: Message):
    if not check_allowed(message):
        return
    PENDING.pop(message.from_user.id, None)
    await message.answer("Cancelled.", reply_markup=menu_kb())

@dp.message(F.text.in_({BTN_EXTRACT, BTN_IMAGES, BTN_COMPRESS}))
async def btn_action(message: Message):
    if not check_allowed(message):
        return

    d = user_dir(message.from_user.id)
    touch_session(d)
    cleanup_old_sessions()

    pdfs = list_pdfs(d)
    if not pdfs:
        await message.answer("No PDFs uploaded. Send PDFs first.", reply_markup=menu_kb())
        return

    action = {
        BTN_EXTRACT: "extract",
        BTN_IMAGES: "images",
        BTN_COMPRESS: "compress",
    }[message.text]

    PENDING[message.from_user.id] = (action, 1, {})
    await message.answer(
        "Send file index (number from /list).\n"
        "Example: 1",
        reply_markup=menu_kb(),
    )

@dp.message(F.text)
async def pending_flow(message: Message):
    # handles interactive steps: file index then param
    if not check_allowed(message):
        return

    user_id = message.from_user.id
    if user_id not in PENDING:
        return

    action, step, payload = PENDING[user_id]
    d = user_dir(user_id)
    touch_session(d)
    cleanup_old_sessions()

    pdfs = list_pdfs(d)
    if not pdfs:
        PENDING.pop(user_id, None)
        await message.answer("No PDFs uploaded.", reply_markup=menu_kb())
        return

    txt = (message.text or "").strip()

    # step 1: choose file
    if step == 1:
        try:
            idx = int(txt)
        except Exception:
            await message.answer("Send a number (file index from /list).", reply_markup=menu_kb())
            return

        if idx < 1 or idx > len(pdfs):
            await message.answer("Invalid index. Use /list.", reply_markup=menu_kb())
            return

        payload["idx"] = idx
        payload["src"] = str(pdfs[idx - 1])
        PENDING[user_id] = (action, 2, payload)

        src = Path(payload["src"])
        if action == "extract":
            await message.answer(
                f"Send ranges to extract from:\n{src.name}\nExamples: 2-4,5,6-10 or 2-",
                reply_markup=menu_kb(),
            )
        elif action == "images":
            await message.answer(
                f"Send DPI (72..400) or 'ok' for default 150.\nSource: {src.name}",
                reply_markup=menu_kb(),
            )
        else:
            await message.answer(
                f"Send preset: screen | ebook | printer | prepress\nSource: {src.name}",
                reply_markup=menu_kb(),
            )
        return

    # step 2: execute
    src = Path(payload["src"])
    if not src.exists():
        PENDING.pop(user_id, None)
        await message.answer("Source file disappeared. Use /list.", reply_markup=menu_kb())
        return

    try:
        if action == "extract":
            ranges = txt
            total = pdf_page_count(src)
            pages0 = parse_ranges(ranges, total)
            pages1 = sorted({p + 1 for p in pages0})
            compact = pages_to_compact_ranges(pages1)

            out = d / f"extract_{src.stem}_{safe_name(ranges)}.pdf"
            extract_pages(src, pages0, out)

            await message.answer(
                "Extract result:\n"
                f"- Source: {src.name}\n"
                f"- Total pages: {total}\n"
                f"- Requested: {ranges}\n"
                f"- Resolved pages ({len(pages1)}): {compact}\n"
                f"- Output: {out.name}",
                reply_markup=menu_kb(),
            )
            await message.answer_document(FSInputFile(out), caption="Extracted PDF", reply_markup=menu_kb())

        elif action == "images":
            dpi = 150
            if txt.lower() != "ok":
                dpi = int(txt)
                dpi = max(72, min(dpi, 400))

            out_dir = d / f"images_{src.stem}_{dpi}dpi"
            if out_dir.exists():
                shutil.rmtree(out_dir, ignore_errors=True)

            imgs = pdf_to_images(src, out_dir, dpi=dpi)
            n = len(imgs)

            if n <= 10:
                await message.answer(f"Sending {n} images as documents (no compression).", reply_markup=menu_kb())
                media = [InputMediaDocument(media=FSInputFile(p)) for p in imgs]
                await message.bot.send_media_group(chat_id=message.chat.id, media=media)
            else:
                zip_path = d / f"{out_dir.name}.zip"
                if zip_path.exists():
                    zip_path.unlink(missing_ok=True)
                shutil.make_archive(str(zip_path.with_suffix("")), "zip", out_dir)
                await message.answer_document(
                    FSInputFile(zip_path),
                    caption=f"Images for {src.name} ({dpi} dpi) — {n} pages (zipped).",
                    reply_markup=menu_kb(),
                )

        else:  # compress
            preset = txt.lower()
            if preset not in {"screen", "ebook", "printer", "prepress"}:
                raise ValueError("Bad preset. Use: screen | ebook | printer | prepress")

            out = d / f"compressed_{preset}_{src.name}"
            compress_pdf(src, out, preset=preset)

            before = src.stat().st_size
            after = out.stat().st_size
            ratio = (after / before) if before else 1.0
            await message.answer_document(
                FSInputFile(out),
                caption=f"Compressed ({preset}). Size: {before/1e6:.1f}MB → {after/1e6:.1f}MB ({ratio:.2f}x)",
                reply_markup=menu_kb(),
            )

    except Exception as e:
        await message.answer(f"Failed:\n{e}", reply_markup=menu_kb())
    finally:
        PENDING.pop(user_id, None)

# =========================
# MAIN
# =========================
async def main():
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

