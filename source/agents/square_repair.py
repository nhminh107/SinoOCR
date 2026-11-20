"""Repair OCR lines containing '□' by cropping user-annotated bboxes and calling `cmd` CLI.

Workflow:
1. Reads `submission/<dataset>/square_bboxes.json`.
2. Crops each bbox from `submission/<dataset>/01_nguon/pages/<trang>.png` with a small padding
   into `outputs/square_repair_crops/<dataset>/<id>.png`.
3. Invokes `cmd -p ...` with a strict prompt that provides ONLY the cropped image,
   the original line containing `□`, and surrounding context lines.
4. Strictly validates the returned JSON:
   - Exact schema: `{"status": "FIXED"|"UNCERTAIN", "fixed_line": "...", "replaced": [...], "reason": "..."}`
   - Must not alter characters outside the `□` positions (checked via regex derived from `text_ocr`).
5. Saves full audit results to `outputs/square_repair_results.json`.
6. Optionally applies validated fixes (`--apply`) to:
   - `submission/<dataset>/nhat_ky_kiem_tra_ocr.csv`
   - `submission/<dataset>/03_text_sach/{han,viet}/<trang>.txt` and merged `{han,viet}.txt`
   (Never modifies `02_ocr_tho/`.)

Example:
    python3 -m source.agents.square_repair --dry-run
    python3 -m source.agents.square_repair --effort high --apply
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from source.square_reviewer.server import (
    BBOX_JSON_FILENAME,
    CSV_FILENAME,
    SquareStore,
    _read_bbox_json,
    _read_csv_rows,
    _write_csv_rows,
)

DEFAULT_SUBMISSION_DIR = Path("submission")
DEFAULT_CROP_DIR = Path("outputs/square_repair_crops")
DEFAULT_RESULT_FILE = Path("outputs/square_repair_results.json")
DEFAULT_DATASETS = (
    "01_QuocTrieuHuongKhoaLuc",
    "02_TuLiemDaiKhoaLuc",
)

_FENCE_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def _extract_json(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        raise ValueError("Agent returned empty output.")

    candidates = [m.group(1) for m in reversed(list(_FENCE_JSON_RE.finditer(text)))]
    candidates.append(text)

    decoder = json.JSONDecoder()
    for cand in candidates:
        pos = 0
        while pos < len(cand):
            idx = cand.find("{", pos)
            if idx == -1:
                break
            try:
                obj, _ = decoder.raw_decode(cand, idx)
                if isinstance(obj, dict) and ("status" in obj or "error" in obj):
                    return obj
            except json.JSONDecodeError:
                pass
            pos = idx + 1
    raise ValueError(f"No valid JSON object found in output: {text[:300]!r}")


def crop_bbox_image(
    source_image: Path,
    bbox: list[int],
    out_path: Path,
    pad: int = 24,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source_image) as im:
        w, h = im.size
        x1, y1, x2, y2 = [int(v) for v in bbox]
        cx1 = max(0, x1 - pad)
        cy1 = max(0, y1 - pad)
        cx2 = min(w, x2 + pad)
        cy2 = min(h, y2 + pad)
        cropped = im.crop((cx1, cy1, cx2, cy2))
        cropped.save(out_path)
    return out_path


def get_line_context(submission_dir: Path, item: dict[str, Any], radius: int = 2) -> list[str]:
    ds_dir = submission_dir / item["tac_pham"]
    lang = item["ngon_ngu"]
    trang = item["trang"]
    clean_p = ds_dir / "03_text_sach" / lang / f"{trang}.txt"
    raw_p = ds_dir / "02_ocr_tho" / lang / f"{trang}.txt"
    src = clean_p if clean_p.is_file() else raw_p
    if not src.is_file():
        return []
    lines = src.read_text(encoding="utf-8").splitlines()
    ln = int(item.get("dong") or 0)
    if ln <= 0:
        return []
    start = max(1, ln - radius)
    end = min(len(lines), ln + radius)
    return [f"Dòng {k}{'*' if k == ln else ''}: {lines[k - 1]}" for k in range(start, end + 1)]


def build_repair_prompt(
    item: dict[str, Any],
    crop_path: Path,
    context_lines: list[str],
) -> str:
    ctx_block = "\n".join(context_lines) if context_lines else "(không có ngữ cảnh thêm)"
    text_ocr = item.get("text_ocr", "")
    lang_label = "Hán–Nôm phồn thể/dị thể" if item.get("ngon_ngu") == "han" else "Tiếng Việt"
    return (
        f"# Hiệu đính ký tự mờ (□) theo vùng BBox đã khoanh\n\n"
        f"- ID lỗi: `{item['id']}`\n"
        f"- Tác phẩm: `{item['tac_pham']}` | Ngôn ngữ: `{lang_label}`\n"
        f"- Ảnh crop phóng to vùng chứa lỗi: `{crop_path.resolve()}`\n"
        f"- Dòng OCR hiện tại (có ký tự `□` cần điền): `{text_ocr}`\n"
        f"- Ngữ cảnh xung quanh:\n{ctx_block}\n\n"
        "## Quy tắc BẮT BUỘC (tuyệt đối tuân thủ)\n"
        "1. Đọc trực tiếp ảnh crop bằng công cụ đọc ảnh một lần duy nhất. Không chạy shell, không đọc/ghi thêm file khác.\n"
        "2. **CHỈ điền/thay thế đúng vị trí dấu `□`** trong `Dòng OCR hiện tại`. **TUYỆT ĐỐI KHÔNG** sửa, thêm, xóa, đổi phồn/giản thể, hay viết lại bất kỳ ký tự nào khác nằm ngoài vị trí `□`.\n"
        "3. Nếu trên ảnh crop ký tự tại vị trí `□` thực sự bị rách, mực nhòe, mất nét không thể nhận diện chắc chắn -> KHÔNG ĐƯỢC tự suy đoán hay bịa chữ theo ngữ cảnh. Hãy trả `\"status\": \"UNCERTAIN\"` và giữ nguyên `□` trong `fixed_line`.\n"
        "4. Nếu trên ảnh crop là dấu chấm/vết mực thừa (không có chữ thực sự tại vị trí `□`), có thể thay `□` bằng chuỗi rỗng `\"\"` và giải thích rõ trong `reason`.\n"
        "5. Chép đúng tự dạng phồn thể / dị thể thực sự xuất hiện trên ảnh (không chuyển sang giản thể Trung Quốc).\n\n"
        "## Định dạng Output\n"
        "Chỉ trả về **một JSON object hợp lệ duy nhất**, không Markdown, không giải thích ngoài JSON:\n"
        "```json\n"
        '{"status":"FIXED","fixed_line":"<dòng hoàn chỉnh giữ nguyên mọi chữ khác>","replaced":["<chữ thay cho mỗi □ theo thứ tự>"],"reason":"<mô tả ngắn gọn nét chữ nhìn thấy trên ảnh>"}\n'
        "```\n"
        "Nếu không đọc chắc chắn, dùng `\"status\":\"UNCERTAIN\"` và giữ nguyên `□` trong `fixed_line`.\n"
        "Nếu không mở được ảnh, trả `{\"error\":\"IMAGE_UNAVAILABLE\"}`.\n"
    )


def validate_fixed_line(text_ocr: str, fixed_line: str) -> tuple[bool, str]:
    """Ensure fixed_line preserves all non-square characters from text_ocr verbatim."""
    if "\n" in fixed_line or "\r" in fixed_line:
        return False, "fixed_line contains newline"
    if "□" not in text_ocr:
        return fixed_line == text_ocr, "text_ocr has no □"
    parts = text_ocr.split("□")
    # Each □ may be replaced by 0..4 characters (usually 1 char, or empty if noise, or multi-char smudge)
    pattern = "^" + ".{0,4}?".join(re.escape(p) for p in parts) + "$"
    if not re.match(pattern, fixed_line):
        return False, (
            f"Modified text outside □ positions: expected pattern matching {text_ocr!r}, got {fixed_line!r}"
        )
    return True, "ok"


def build_cmd_command(
    cmd_bin: str,
    prompt_text: str,
    max_turns: int,
    model: str | None = None,
    effort: str | None = None,
) -> list[str]:
    command = [
        cmd_bin,
        "-t",
        "--no-session",
        "--skip-onboarding",
        "--no-auto-update",
        "--no-skills",
        "--safe-mode",
        "--max-turns",
        str(max_turns),
    ]
    if model:
        command.extend(["-m", model])
    if effort:
        command.extend(["--effort", effort])
    command.extend(["-p", prompt_text])
    return command


def process_one(
    item: dict[str, Any],
    submission_dir: Path,
    crop_dir: Path,
    cmd_bin: str,
    model: str | None,
    effort: str | None,
    max_turns: int,
    timeout: int,
    pad: int,
    retries: int,
) -> dict[str, Any]:
    src_img = submission_dir.parent / item["image_path"]
    crop_path = crop_dir / item["tac_pham"] / f"{item['id']}.png"
    crop_bbox_image(src_img, item["bbox"], crop_path, pad=pad)

    context_lines = get_line_context(submission_dir, item)
    prompt = build_repair_prompt(item, crop_path, context_lines)
    cmd = build_cmd_command(cmd_bin, prompt, max_turns=max_turns, model=model, effort=effort)

    t0 = time.monotonic()
    last_error = ""
    for attempt in range(1, retries + 2):
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if proc.returncode != 0:
                last_error = f"rc={proc.returncode}: {(proc.stderr or proc.stdout).strip()[:250]}"
                continue
            parsed = _extract_json(proc.stdout)
            if "error" in parsed:
                last_error = f"agent error: {parsed['error']}"
                continue

            status = str(parsed.get("status", "")).upper()
            fixed_line = str(parsed.get("fixed_line", ""))
            replaced = parsed.get("replaced", [])
            reason = str(parsed.get("reason", "")).strip()

            if status not in ("FIXED", "UNCERTAIN"):
                last_error = f"invalid status: {status!r}"
                continue

            ok, msg = validate_fixed_line(item["text_ocr"], fixed_line)
            if not ok:
                last_error = f"validation failed: {msg}"
                continue

            if status == "FIXED" and "□" in fixed_line:
                status = "UNCERTAIN"

            return {
                "id": item["id"],
                "tac_pham": item["tac_pham"],
                "ngon_ngu": item["ngon_ngu"],
                "trang": item["trang"],
                "dong": item["dong"],
                "row_index": item["row_index"],
                "error_index": item["error_index"],
                "crop_path": str(crop_path),
                "text_ocr": item["text_ocr"],
                "status": status,
                "fixed_line": fixed_line,
                "replaced": replaced,
                "reason": reason,
                "attempts": attempt,
                "elapsed_s": round(time.monotonic() - t0, 2),
                "ok": True,
            }
        except subprocess.TimeoutExpired:
            last_error = f"timeout after {timeout}s"
        except Exception as exc:
            last_error = str(exc)

    return {
        "id": item["id"],
        "tac_pham": item["tac_pham"],
        "ngon_ngu": item["ngon_ngu"],
        "trang": item["trang"],
        "dong": item["dong"],
        "row_index": item["row_index"],
        "error_index": item["error_index"],
        "crop_path": str(crop_path),
        "text_ocr": item["text_ocr"],
        "status": "ERROR",
        "fixed_line": item["text_ocr"],
        "replaced": [],
        "reason": last_error,
        "attempts": retries + 1,
        "elapsed_s": round(time.monotonic() - t0, 2),
        "ok": False,
    }


def apply_results(submission_dir: Path, results: list[dict[str, Any]]) -> dict[str, int]:
    """Write validated results into CSV log and 03_text_sach/ without touching 02_ocr_tho/."""
    store = SquareStore(submission_dir)
    by_page: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for r in results:
        if not r.get("ok"):
            continue
        by_page.setdefault((r["tac_pham"], r["ngon_ngu"], r["trang"]), []).append(r)

    applied_fixed = 0
    applied_uncertain = 0
    for (tac_pham, ngon_ngu, trang), page_results in by_page.items():
        # If multiple bboxes point to the same row_index, merge sequentially
        updates = []
        for r in page_results:
            if r["row_index"] is None:
                continue
            if r["status"] == "FIXED":
                trang_thai = "DA_SUA"
                applied_fixed += 1
            else:
                trang_thai = "GIU_NGUYEN_MO"
                applied_uncertain += 1
            updates.append(
                {
                    "row_index": r["row_index"],
                    "text_sua": r["fixed_line"],
                    "trang_thai": trang_thai,
                    "ghi_chu": f"[cmd repair] {r['reason']}".strip(),
                }
            )
        if updates:
            store.save_updates(
                tac_pham=tac_pham,
                ngon_ngu=ngon_ngu,
                trang=trang,
                updates=updates,
                bboxes=None,
                apply_to_txt=True,
            )
    return {"fixed": applied_fixed, "uncertain": applied_uncertain}


def main() -> None:
    parser = argparse.ArgumentParser(description="OCR lại đúng các vùng BBox lỗi □ bằng Command Code (cmd).")
    parser.add_argument("--submission-dir", type=Path, default=DEFAULT_SUBMISSION_DIR)
    parser.add_argument("--crop-dir", type=Path, default=DEFAULT_CROP_DIR)
    parser.add_argument("--result-file", type=Path, default=DEFAULT_RESULT_FILE)
    parser.add_argument("--datasets", nargs="+", default=list(DEFAULT_DATASETS))
    parser.add_argument("--cmd-bin", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", default=None, choices=("off", "low", "high", "max"))
    parser.add_argument("--max-turns", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--pad", type=int, default=24)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Ghi kết quả hợp lệ vào CSV và 03_text_sach/.")
    args = parser.parse_args()

    items: list[dict[str, Any]] = []
    for ds in args.datasets:
        bbox_json = args.submission_dir / ds / BBOX_JSON_FILENAME
        items.extend(_read_bbox_json(bbox_json))

    if not items:
        print("Không tìm thấy bbox nào trong square_bboxes.json.")
        return

    print(f"Tìm thấy {len(items)} vùng bbox cần OCR lại.")
    if args.dry_run:
        for item in items:
            src_img = args.submission_dir.parent / item["image_path"]
            crop_path = args.crop_dir / item["tac_pham"] / f"{item['id']}.png"
            crop_bbox_image(src_img, item["bbox"], crop_path, pad=args.pad)
            print(f"  [DRY-RUN] {item['id']} | Dòng {item['dong']} | {item['text_ocr']} -> {crop_path}")
        return

    cmd_bin = args.cmd_bin or shutil.which("cmd") or shutil.which("command-code")
    if not cmd_bin:
        raise SystemExit("Không tìm thấy lệnh `cmd` hoặc `command-code` trong PATH.")

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = {
            pool.submit(
                process_one,
                item,
                args.submission_dir,
                args.crop_dir,
                cmd_bin,
                args.model,
                args.effort,
                args.max_turns,
                args.timeout,
                args.pad,
                args.retries,
            ): item
            for item in items
        }
        for fut in as_completed(futs):
            res = fut.result()
            results.append(res)
            print(
                f"[{res['status']:9s}] {res['id']} ({res['elapsed_s']}s) | "
                f"{res['text_ocr']} -> {res['fixed_line']} | {res['reason']}"
            )

    results.sort(key=lambda r: (r["tac_pham"], r["ngon_ngu"], int(re.search(r"(\d+)", r["trang"]).group(1)), r["error_index"]))
    args.result_file.parent.mkdir(parents=True, exist_ok=True)
    args.result_file.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "total": len(results),
                "fixed": sum(1 for r in results if r["status"] == "FIXED"),
                "uncertain": sum(1 for r in results if r["status"] == "UNCERTAIN"),
                "errors": sum(1 for r in results if r["status"] == "ERROR"),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nĐã lưu báo cáo chi tiết tại: {args.result_file}")

    if args.apply:
        stats = apply_results(args.submission_dir, results)
        print(f"Đã cập nhật CSV + 03_text_sach/: fixed={stats['fixed']}, uncertain={stats['uncertain']}")


if __name__ == "__main__":
    main()
