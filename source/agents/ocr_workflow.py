"""AI Workflow for step 02_ocr_tho using Command Code (`cmd`) CLI.

Reads packed MaxRects images and region IDs from `outputs/packed_maxrects/manifest.json`,
loads the dataset-specific prompt from `source/agents/prompts/<dataset>.md`, invokes
Command Code (`cmd -p ...`) to return per-region JSON (`{"1":{"h":"...","v":"..."}, ...}`),
validates, retries on failure, sorts region IDs, merges Han/Viet text, and writes
`submission/<dataset>/02_ocr_tho/{han,viet}/<page>.txt` via `source.agents.tools`.

Example usage:
    python -m source.agents.ocr_workflow --datasets 02_TuLiemDaiKhoaLuc --pages page2_1.png --dry-run
    python -m source.agents.ocr_workflow --datasets 02_TuLiemDaiKhoaLuc --pages page2_1.png --effort low
    python -m source.agents.ocr_workflow --effort low --batch-size 3 --workers 4 --retries 2
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .tools import save_han_viet_files

DEFAULT_INPUT_DIR = Path("outputs/packed_maxrects")
DEFAULT_MANIFEST_NAME = "manifest.json"
DEFAULT_OUTPUT_DIR = Path("submission")
DEFAULT_PROMPTS_DIR = Path("source/agents/prompts")
DEFAULT_LOG_FILE = Path("outputs/ocr_workflow_log.jsonl")
DEFAULT_DATASETS = (
    "01_QuocTrieuHuongKhoaLuc",
    "02_TuLiemDaiKhoaLuc",
)

_NATURAL_SPLIT_RE = re.compile(r"(\d+)")
_FENCE_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_ALLOWED_REGION_KEYS = frozenset(("h", "v", "s"))
_LOG_LOCK = threading.Lock()


@dataclass(frozen=True)
class PageJob:
    dataset: str
    image_path: Path
    prompt_path: Path
    han_dir: Path
    viet_dir: Path
    output_filename: str
    region_ids: Tuple[int, ...]


def natural_sort_key(name: str) -> List[Tuple[int, Any]]:
    parts = _NATURAL_SPLIT_RE.split(name)
    key: List[Tuple[int, Any]] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part.lower()))
    return key


def load_manifest_regions(manifest_path: Path) -> Dict[Tuple[str, str], Tuple[int, ...]]:
    """Load sorted region indices per (dataset, filename) from packed manifest.json."""
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Manifest file not found: {manifest_path}")

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    pages = data.get("pages")
    if not isinstance(pages, list):
        raise ValueError(f"Invalid manifest format (missing 'pages' list): {manifest_path}")

    region_map: Dict[Tuple[str, str], Tuple[int, ...]] = {}
    for entry in pages:
        if not isinstance(entry, dict) or entry.get("status") != "ok":
            continue
        dataset = str(entry.get("dataset") or "")
        filename = str(entry.get("filename") or "")
        regions = entry.get("regions") or []
        indices = sorted(int(r["index"]) for r in regions if isinstance(r, dict) and "index" in r)
        if dataset and filename and indices:
            region_map[(dataset, filename)] = tuple(indices)
    return region_map


def render_prompt(template_text: str, context: Dict[str, str]) -> str:
    rendered = template_text
    for key, value in context.items():
        rendered = rendered.replace(f"{{{key}}}", value)
    return rendered


def format_region_ids(region_ids: Sequence[int]) -> str:
    return ", ".join(str(rid) for rid in region_ids)


def build_single_prompt(template_text: str, job: PageJob) -> str:
    return render_prompt(
        template_text,
        {
            "dataset": job.dataset,
            "image_path": str(job.image_path),
            "image_filename": job.image_path.name,
            "output_filename": job.output_filename,
            "region_ids": format_region_ids(job.region_ids),
            "han_dir": str(job.han_dir),
            "viet_dir": str(job.viet_dir),
        },
    )


def build_batch_prompt(template_text: str, batch: Sequence[PageJob]) -> str:
    first = batch[0]
    base_prompt = render_prompt(
        template_text,
        {
            "dataset": first.dataset,
            "image_path": "(xem danh sách ảnh bên dưới)",
            "image_filename": "(xem danh sách ảnh bên dưới)",
            "output_filename": "(tương ứng từng ảnh)",
            "region_ids": "(xem danh sách ID tương ứng từng ảnh bên dưới)",
            "han_dir": str(first.han_dir),
            "viet_dir": str(first.viet_dir),
        },
    )
    items_desc = "\n".join(
        f"{idx}. `{job.image_path.name}` | Ảnh: `{job.image_path}` | Region ID: `{format_region_ids(job.region_ids)}`"
        for idx, job in enumerate(batch, start=1)
    )
    batch_override = (
        "\n\n## Chế độ Batch (nhiều ảnh trong 1 phiên)\n"
        "Xử lý tất cả các ảnh sau theo đúng quy tắc ở trên:\n"
        f"{items_desc}\n\n"
        "Thay vì trả 1 JSON phẳng cho 1 ảnh, chỉ trả **một JSON object hợp lệ, gọn, không Markdown hay giải thích**, "
        "với key ngoài cùng là tên file ảnh (ví dụ `\"page1_1.png\"`) và value là object theo đúng schema vùng của ảnh đó:\n"
        "```json\n"
        '{"page1_1.png":{"1":{"h":"..."},"2":{"v":"..."}},"page1_2.png":{"1":{"s":1},"2":{"v":"..."}}}\n'
        "```\n"
        "Nếu một ảnh không truy cập được, đặt value của ảnh đó là `{\"error\":\"IMAGE_UNAVAILABLE\"}`.\n"
    )
    return base_prompt + batch_override


def _reject_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    obj: Dict[str, Any] = {}
    for key, val in pairs:
        if key in obj:
            raise ValueError(f"Duplicate JSON key: {key!r}")
        obj[key] = val
    return obj


def _extract_json_objects(stdout: str) -> List[Dict[str, Any]]:
    text = stdout.strip()
    if not text:
        raise ValueError("Agent returned empty output.")

    raw_candidates: List[str] = [
        match.group(1) for match in reversed(list(_FENCE_JSON_RE.finditer(text)))
    ]
    raw_candidates.append(text)

    decoder = json.JSONDecoder()
    objects: List[Dict[str, Any]] = []
    for candidate in raw_candidates:
        pos = 0
        while pos < len(candidate):
            idx = candidate.find("{", pos)
            if idx == -1:
                break
            try:
                _, end = decoder.raw_decode(candidate, idx)
            except json.JSONDecodeError:
                pos = idx + 1
                continue
            raw_slice = candidate[idx:end]
            obj = json.loads(raw_slice, object_pairs_hook=_reject_duplicate_keys)
            if isinstance(obj, dict):
                objects.append(obj)
            pos = end
    return objects


def validate_region_map(
    obj: Any,
    expected_ids: Sequence[int],
) -> Dict[str, Dict[str, Any]]:
    """Validate per-region JSON object against requested region IDs and schema."""
    if not isinstance(obj, dict):
        raise ValueError("OCR output must be a JSON object.")
    if obj.get("error") == "IMAGE_UNAVAILABLE":
        raise ValueError("Agent reported IMAGE_UNAVAILABLE.")

    expected_keys = {str(rid) for rid in expected_ids}
    actual_keys = set(obj.keys())
    missing = sorted(expected_keys - actual_keys, key=int)
    extra = sorted(actual_keys - expected_keys)
    if missing or extra:
        raise ValueError(f"Region ID mismatch (missing={missing}, extra={extra}).")

    validated: Dict[str, Dict[str, Any]] = {}
    for rid in expected_ids:
        key = str(rid)
        entry = obj[key]
        if not isinstance(entry, dict):
            raise ValueError(f"Region '{key}' value must be a JSON object.")

        unknown = set(entry.keys()) - _ALLOWED_REGION_KEYS
        if unknown:
            raise ValueError(f"Region '{key}' contains unsupported keys: {sorted(unknown)}")

        if "s" in entry:
            if entry["s"] != 1 or "h" in entry or "v" in entry:
                raise ValueError(f"Region '{key}' with 's' must be exactly {{\"s\": 1}}.")
        else:
            if "h" in entry and not isinstance(entry["h"], str):
                raise ValueError(f"Region '{key}' field 'h' must be a string.")
            if "v" in entry and not isinstance(entry["v"], str):
                raise ValueError(f"Region '{key}' field 'v' must be a string.")

        validated[key] = entry
    return validated


def extract_single_region_map(
    stdout: str,
    expected_ids: Sequence[int],
) -> Dict[str, Dict[str, Any]]:
    objects = _extract_json_objects(stdout)
    if not objects:
        raise ValueError("No valid JSON object found in agent output.")

    last_error: Optional[Exception] = None
    for obj in objects:
        if obj.get("error") == "IMAGE_UNAVAILABLE":
            raise ValueError("Agent reported IMAGE_UNAVAILABLE.")
        try:
            return validate_region_map(obj, expected_ids)
        except ValueError as exc:
            last_error = exc
    raise ValueError(str(last_error) if last_error else "Invalid region JSON output.")


def extract_batch_region_maps(
    stdout: str,
    batch: Sequence[PageJob],
) -> Dict[str, Any]:
    """Extract outer {image_name: region_map} dict from batch agent output."""
    objects = _extract_json_objects(stdout)
    if not objects:
        raise ValueError("No valid JSON object found in batch agent output.")

    expected_by_name = {job.image_path.name: job.image_path.name for job in batch}
    expected_by_stem = {job.image_path.stem: job.image_path.name for job in batch}

    for obj in objects:
        if obj.get("error") == "IMAGE_UNAVAILABLE":
            raise ValueError("Agent reported IMAGE_UNAVAILABLE.")
        container = obj.get("pages") if isinstance(obj.get("pages"), dict) else obj
        if not isinstance(container, dict):
            continue
        mapped: Dict[str, Any] = {}
        for raw_key, val in container.items():
            key_name = Path(str(raw_key).strip()).name
            canonical = expected_by_name.get(key_name) or expected_by_stem.get(Path(key_name).stem)
            if canonical is not None:
                mapped[canonical] = val
        if mapped:
            return mapped

    raise ValueError("Could not find per-image JSON map in batch agent output.")


def merge_region_map(
    region_map: Dict[str, Dict[str, Any]],
    expected_ids: Sequence[int],
) -> Tuple[str, str, int, int]:
    """Sort region IDs ascending and merge Han ('h') and Viet ('v') text."""
    han_parts: List[str] = []
    viet_parts: List[str] = []
    skipped_regions = 0

    for rid in sorted(expected_ids):
        entry = region_map[str(rid)]
        if entry.get("s") == 1:
            skipped_regions += 1
            continue
        h_val = entry.get("h")
        if isinstance(h_val, str) and h_val.strip():
            han_parts.append(h_val.strip("\n"))
        v_val = entry.get("v")
        if isinstance(v_val, str) and v_val.strip():
            viet_parts.append(v_val.strip("\n"))

    han_text = "\n".join(han_parts)
    viet_text = "\n".join(viet_parts)
    uncertain_count = han_text.count("□") + viet_text.count("□")
    return han_text, viet_text, skipped_regions, uncertain_count


def collect_jobs(
    input_dir: Path,
    manifest_path: Path,
    output_dir: Path,
    prompts_dir: Path,
    datasets: Sequence[str],
    pages: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
) -> List[PageJob]:
    manifest_regions = load_manifest_regions(manifest_path)
    page_filter = set(pages) if pages else None
    jobs: List[PageJob] = []

    for dataset in datasets:
        dataset_dir = input_dir / dataset
        if not dataset_dir.is_dir():
            raise FileNotFoundError(f"Dataset image directory not found: {dataset_dir}")

        prompt_path = prompts_dir / f"{dataset}.md"
        if not prompt_path.is_file():
            raise FileNotFoundError(f"Prompt file not found for dataset '{dataset}': {prompt_path}")

        png_files = sorted(
            (p for p in dataset_dir.iterdir() if p.is_file() and p.suffix.lower() == ".png"),
            key=lambda p: natural_sort_key(p.name),
        )
        if page_filter is not None:
            png_files = [
                p for p in png_files if p.name in page_filter or p.stem in page_filter
            ]
        if limit is not None:
            png_files = png_files[:limit]

        han_dir = output_dir / dataset / "02_ocr_tho" / "han"
        viet_dir = output_dir / dataset / "02_ocr_tho" / "viet"

        for image_path in png_files:
            region_ids = manifest_regions.get((dataset, image_path.name))
            if not region_ids:
                raise ValueError(
                    f"Missing region IDs in {manifest_path} for {dataset}/{image_path.name}"
                )
            jobs.append(
                PageJob(
                    dataset=dataset,
                    image_path=image_path,
                    prompt_path=prompt_path,
                    han_dir=han_dir,
                    viet_dir=viet_dir,
                    output_filename=f"{image_path.stem}.txt",
                    region_ids=region_ids,
                )
            )

    return jobs


def group_batches(jobs: Sequence[PageJob], batch_size: int) -> List[List[PageJob]]:
    size = max(1, batch_size)
    batches: List[List[PageJob]] = []
    current: List[PageJob] = []
    current_dataset: Optional[str] = None

    for job in jobs:
        if current and (job.dataset != current_dataset or len(current) >= size):
            batches.append(current)
            current = []
        current.append(job)
        current_dataset = job.dataset

    if current:
        batches.append(current)
    return batches


def build_cmd_command(
    cmd_bin: str,
    prompt_text: str,
    max_turns: int,
    model: Optional[str] = None,
    effort: Optional[str] = None,
) -> List[str]:
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


def _append_log(log_file: Path, record: Dict[str, Any]) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with _LOG_LOCK:
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _save_validated_regions(
    job: PageJob,
    region_map: Dict[str, Dict[str, Any]],
    elapsed: float,
    attempts: int,
) -> Dict[str, Any]:
    han_text, viet_text, skipped_regions, uncertain_count = merge_region_map(
        region_map, job.region_ids
    )
    save_result = save_han_viet_files(
        han_dir=str(job.han_dir),
        viet_dir=str(job.viet_dir),
        han_text=han_text,
        viet_text=viet_text,
        han_filename=job.output_filename,
        viet_filename=job.output_filename,
        overwrite=True,
    )
    if not save_result.get("ok"):
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dataset": job.dataset,
            "image": job.image_path.name,
            "status": "error",
            "elapsed_s": elapsed,
            "attempts": attempts,
            "error": f"Failed to save OCR files: {save_result}",
        }

    han_info = save_result["files"]["han"]
    viet_info = save_result["files"]["viet"]
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dataset": job.dataset,
        "image": job.image_path.name,
        "status": "ok",
        "elapsed_s": elapsed,
        "attempts": attempts,
        "regions_total": len(job.region_ids),
        "regions_skipped": skipped_regions,
        "han_path": han_info["path"],
        "han_lines": han_info["lines"],
        "viet_path": viet_info["path"],
        "viet_lines": viet_info["lines"],
        "uncertain_count": uncertain_count,
    }


def run_single_job_with_retry(
    job: PageJob,
    project_root: Path,
    cmd_bin: str,
    max_turns: int,
    timeout: int,
    retries: int,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    start_attempt: int = 1,
) -> Dict[str, Any]:
    template_text = job.prompt_path.read_text(encoding="utf-8")
    prompt_text = build_single_prompt(template_text, job)
    command = build_cmd_command(
        cmd_bin=cmd_bin,
        prompt_text=prompt_text,
        max_turns=max_turns,
        model=model,
        effort=effort,
    )

    total_attempts = max(1, 1 + retries)
    last_record: Dict[str, Any] = {}

    for attempt_offset in range(total_attempts):
        attempt_num = start_attempt + attempt_offset
        start = time.monotonic()
        try:
            proc = subprocess.run(
                command,
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            last_record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "dataset": job.dataset,
                "image": job.image_path.name,
                "status": "timeout",
                "elapsed_s": round(time.monotonic() - start, 2),
                "attempts": attempt_num,
                "error": f"Command timed out after {timeout}s",
            }
            continue

        elapsed = round(time.monotonic() - start, 2)
        if proc.returncode != 0:
            last_record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "dataset": job.dataset,
                "image": job.image_path.name,
                "status": "error",
                "elapsed_s": elapsed,
                "attempts": attempt_num,
                "returncode": proc.returncode,
                "error": (proc.stderr or proc.stdout or "").strip()[-1500:],
            }
            continue

        try:
            region_map = extract_single_region_map(proc.stdout, job.region_ids)
        except ValueError as exc:
            last_record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "dataset": job.dataset,
                "image": job.image_path.name,
                "status": "error",
                "elapsed_s": elapsed,
                "attempts": attempt_num,
                "returncode": proc.returncode,
                "error": str(exc),
                "stdout": proc.stdout.strip()[-1500:],
            }
            continue

        return _save_validated_regions(job, region_map, elapsed, attempts=attempt_num)

    return last_record


def run_batch(
    batch: Sequence[PageJob],
    project_root: Path,
    cmd_bin: str,
    max_turns: int,
    timeout: int,
    retries: int,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    first = batch[0]
    template_text = first.prompt_path.read_text(encoding="utf-8")

    if len(batch) == 1:
        if dry_run:
            prompt_text = build_single_prompt(template_text, first)
            command = build_cmd_command(cmd_bin, prompt_text, max_turns, model, effort)
            return [
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "dataset": first.dataset,
                    "image": first.image_path.name,
                    "status": "dry_run",
                    "elapsed_s": 0.0,
                    "command_preview": [
                        c if c != prompt_text else "<RENDERED_PROMPT>" for c in command
                    ],
                    "prompt": prompt_text,
                }
            ]
        return [
            run_single_job_with_retry(
                job=first,
                project_root=project_root,
                cmd_bin=cmd_bin,
                max_turns=max_turns,
                timeout=timeout,
                retries=retries,
                model=model,
                effort=effort,
            )
        ]

    prompt_text = build_batch_prompt(template_text, batch)
    effective_turns = max(max_turns, len(batch) + 3)
    command = build_cmd_command(cmd_bin, prompt_text, effective_turns, model, effort)

    if dry_run:
        return [
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "dataset": first.dataset,
                "image": ", ".join(j.image_path.name for j in batch),
                "status": "dry_run",
                "elapsed_s": 0.0,
                "command_preview": [
                    c if c != prompt_text else "<RENDERED_PROMPT>" for c in command
                ],
                "prompt": prompt_text,
            }
        ]

    start = time.monotonic()
    batch_error: Optional[str] = None
    batch_status = "error"
    batch_stdout = ""
    batch_maps: Dict[str, Any] = {}

    try:
        proc = subprocess.run(
            command,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = round(time.monotonic() - start, 2)
        batch_stdout = proc.stdout.strip()[-1500:]
        if proc.returncode != 0:
            batch_error = (proc.stderr or proc.stdout or "").strip()[-1500:]
        else:
            try:
                batch_maps = extract_batch_region_maps(proc.stdout, batch)
            except ValueError as exc:
                batch_error = str(exc)
    except subprocess.TimeoutExpired:
        elapsed = round(time.monotonic() - start, 2)
        batch_status = "timeout"
        batch_error = f"Batch command timed out after {timeout}s"

    per_image_elapsed = round(elapsed / len(batch), 2)
    results: List[Dict[str, Any]] = []

    for job in batch:
        page_err = batch_error
        if page_err is None:
            raw_entry = batch_maps.get(job.image_path.name)
            if raw_entry is None:
                page_err = f"Missing entry for '{job.image_path.name}' in batch JSON response."
            else:
                try:
                    validated = validate_region_map(raw_entry, job.region_ids)
                except ValueError as exc:
                    page_err = str(exc)
                else:
                    results.append(
                        _save_validated_regions(job, validated, per_image_elapsed, attempts=1)
                    )
                    continue

        if retries > 0:
            retry_result = run_single_job_with_retry(
                job=job,
                project_root=project_root,
                cmd_bin=cmd_bin,
                max_turns=max_turns,
                timeout=timeout,
                retries=retries - 1,
                model=model,
                effort=effort,
                start_attempt=2,
            )
            results.append(retry_result)
        else:
            results.append(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "dataset": job.dataset,
                    "image": job.image_path.name,
                    "status": batch_status,
                    "elapsed_s": per_image_elapsed,
                    "attempts": 1,
                    "error": page_err or "Batch failed.",
                    "stdout": batch_stdout,
                }
            )
    return results


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m source.agents.ocr_workflow",
        description="Run 02_ocr_tho batch OCR workflow via Command Code (`cmd`) CLI.",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help="Root directory of packed images (default: outputs/packed_maxrects).",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to packed manifest.json (default: <input-dir>/manifest.json).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Submission root directory (default: submission).",
    )
    parser.add_argument(
        "--prompts-dir",
        type=Path,
        default=DEFAULT_PROMPTS_DIR,
        help="Directory containing <dataset>.md prompt files (default: source/agents/prompts).",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=DEFAULT_LOG_FILE,
        help="JSONL log path (default: outputs/ocr_workflow_log.jsonl).",
    )
    parser.add_argument(
        "--datasets",
        nargs="+",
        default=list(DEFAULT_DATASETS),
        help="Datasets to process (default: both datasets).",
    )
    parser.add_argument(
        "--pages",
        nargs="+",
        default=None,
        help="Optional specific page filenames or stems (e.g. page1_1.png page2_1).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of pages per dataset.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-run and overwrite pages that already have output files.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1,
        help="Number of images to process per Command Code session (default: 1).",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=1,
        help="Number of retries per failed/invalid page (default: 1).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of concurrent Command Code workers (default: 1).",
    )
    parser.add_argument(
        "--cmd-bin",
        default="cmd",
        help="Command Code CLI binary name or path (default: cmd).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Optional model override passed to `cmd -m`.",
    )
    parser.add_argument(
        "--effort",
        default=None,
        help="Optional reasoning effort passed to `cmd --effort` (e.g. low, medium, high).",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=5,
        help="Base max agent turns per session (default: 5; auto-scaled for batches).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Per-session timeout in seconds (default: 300).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print command and rendered prompt without executing `cmd`.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    project_root = Path.cwd().resolve()
    manifest_path = args.manifest or (args.input_dir / DEFAULT_MANIFEST_NAME)

    if not args.dry_run and shutil.which(args.cmd_bin) is None:
        print(f"Error: Command Code CLI '{args.cmd_bin}' not found in PATH.", file=sys.stderr)
        return 2

    all_jobs = collect_jobs(
        input_dir=args.input_dir,
        manifest_path=manifest_path,
        output_dir=args.output_dir,
        prompts_dir=args.prompts_dir,
        datasets=args.datasets,
        pages=args.pages,
        limit=args.limit,
    )
    if not all_jobs:
        print("No matching images found.")
        return 0

    total = len(all_jobs)
    counts = {"ok": 0, "skipped": 0, "error": 0, "timeout": 0, "dry_run": 0}
    done_counter = 0

    pending_jobs: List[PageJob] = []
    for job in all_jobs:
        han_target = job.han_dir / job.output_filename
        viet_target = job.viet_dir / job.output_filename
        if not args.overwrite and (han_target.exists() or viet_target.exists()):
            done_counter += 1
            counts["skipped"] += 1
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "dataset": job.dataset,
                "image": job.image_path.name,
                "status": "skipped",
                "elapsed_s": 0.0,
                "han_exists": han_target.exists(),
                "viet_exists": viet_target.exists(),
            }
            if not args.dry_run:
                _append_log(args.log_file, record)
            print(f"[{done_counter}/{total}] {job.dataset}/{job.image_path.name} -> SKIPPED (already exists)")
        else:
            pending_jobs.append(job)

    batches = group_batches(pending_jobs, args.batch_size)
    workers = max(1, args.workers)
    retries = max(0, args.retries)

    def _report_batch(results: List[Dict[str, Any]]) -> None:
        nonlocal done_counter
        for result in results:
            status = result["status"]
            counts[status] = counts.get(status, 0) + 1
            if not args.dry_run:
                _append_log(args.log_file, result)
            with _LOG_LOCK:
                if status != "dry_run":
                    done_counter += 1
                    prefix = f"[{done_counter}/{total}] {result['dataset']}/{result['image']}"
                else:
                    prefix = f"[BATCH] {result['dataset']}: {result['image']}"

                if status == "dry_run":
                    print(f"{prefix} -> DRY RUN")
                    print("CMD:", " ".join(result["command_preview"]))
                    print("--- PROMPT ---")
                    print(result["prompt"])
                    print("--------------")
                elif status == "ok":
                    uncertain = result.get("uncertain_count", 0)
                    flag = f" □x{uncertain}" if uncertain else ""
                    skip_note = (
                        f" skip={result['regions_skipped']}"
                        if result.get("regions_skipped")
                        else ""
                    )
                    print(
                        f"{prefix} -> OK ({result['elapsed_s']}s, try={result['attempts']}) "
                        f"han={result['han_lines']}L viet={result['viet_lines']}L{skip_note}{flag}"
                    )
                else:
                    print(
                        f"{prefix} -> {status.upper()} ({result['elapsed_s']}s, try={result.get('attempts', 1)}): "
                        f"{result.get('error', '')}",
                        file=sys.stderr,
                    )

    if workers == 1 or args.dry_run:
        for batch in batches:
            results = run_batch(
                batch=batch,
                project_root=project_root,
                cmd_bin=args.cmd_bin,
                max_turns=args.max_turns,
                timeout=args.timeout,
                retries=retries,
                model=args.model,
                effort=args.effort,
                dry_run=args.dry_run,
            )
            _report_batch(results)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    run_batch,
                    batch,
                    project_root,
                    args.cmd_bin,
                    args.max_turns,
                    args.timeout,
                    retries,
                    args.model,
                    args.effort,
                    args.dry_run,
                )
                for batch in batches
            ]
            for fut in as_completed(futures):
                _report_batch(fut.result())

    print(
        f"Done. total={total} ok={counts['ok']} skipped={counts['skipped']} "
        f"error={counts['error']} timeout={counts['timeout']}"
    )
    return 0 if (counts["error"] == 0 and counts["timeout"] == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
