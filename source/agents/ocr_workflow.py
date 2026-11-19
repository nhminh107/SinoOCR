"""AI Workflow for step 02_ocr_tho using Command Code (`cmd`) CLI.

Reads packed images from `outputs/packed_maxrects/<dataset>/`, loads the
dataset-specific prompt from `source/agents/prompts/<dataset>.md`, invokes
Command Code (`cmd -p ...`), and writes raw OCR outputs into
`submission/<dataset>/02_ocr_tho/{han,viet}/<page>.txt` via `source.agents.tools`.

Example usage:
    python -m source.agents.ocr_workflow --datasets 02_TuLiemDaiKhoaLuc --limit 1 --dry-run
    python -m source.agents.ocr_workflow --datasets 02_TuLiemDaiKhoaLuc --pages page2_1.png
    python -m source.agents.ocr_workflow --workers 2
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

DEFAULT_INPUT_DIR = Path("outputs/packed_maxrects")
DEFAULT_OUTPUT_DIR = Path("submission")
DEFAULT_PROMPTS_DIR = Path("source/agents/prompts")
DEFAULT_LOG_FILE = Path("outputs/ocr_workflow_log.jsonl")
DEFAULT_DATASETS = (
    "01_QuocTrieuHuongKhoaLuc",
    "02_TuLiemDaiKhoaLuc",
)

_NATURAL_SPLIT_RE = re.compile(r"(\d+)")
_LOG_LOCK = threading.Lock()


@dataclass(frozen=True)
class PageJob:
    dataset: str
    image_path: Path
    prompt_path: Path
    han_dir: Path
    viet_dir: Path
    output_filename: str


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


def render_prompt(template_text: str, context: Dict[str, str]) -> str:
    rendered = template_text
    for key, value in context.items():
        rendered = rendered.replace(f"{{{key}}}", value)
    return rendered


def collect_jobs(
    input_dir: Path,
    output_dir: Path,
    prompts_dir: Path,
    datasets: Sequence[str],
    pages: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
) -> List[PageJob]:
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
            jobs.append(
                PageJob(
                    dataset=dataset,
                    image_path=image_path,
                    prompt_path=prompt_path,
                    han_dir=han_dir,
                    viet_dir=viet_dir,
                    output_filename=f"{image_path.stem}.txt",
                )
            )

    return jobs


def build_cmd_command(
    cmd_bin: str,
    prompt_text: str,
    permission_mode: str,
    max_turns: int,
    model: Optional[str] = None,
    effort: Optional[str] = None,
) -> List[str]:
    command = [
        cmd_bin,
        "-p",
        prompt_text,
        "-t",
        "--no-session",
        "--skip-onboarding",
        "--no-auto-update",
        "--permission-mode",
        permission_mode,
        "--max-turns",
        str(max_turns),
    ]
    if model:
        command.extend(["-m", model])
    if effort:
        command.extend(["--effort", effort])
    return command


def _append_log(log_file: Path, record: Dict[str, Any]) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with _LOG_LOCK:
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def run_single_job(
    job: PageJob,
    project_root: Path,
    cmd_bin: str,
    python_bin: str,
    permission_mode: str,
    max_turns: int,
    timeout: int,
    overwrite: bool,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    han_target = job.han_dir / job.output_filename
    viet_target = job.viet_dir / job.output_filename

    if not overwrite and (han_target.exists() or viet_target.exists()):
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dataset": job.dataset,
            "image": job.image_path.name,
            "status": "skipped",
            "elapsed_s": 0.0,
            "han_exists": han_target.exists(),
            "viet_exists": viet_target.exists(),
        }

    template_text = job.prompt_path.read_text(encoding="utf-8")
    context = {
        "dataset": job.dataset,
        "image_path": str(job.image_path),
        "image_filename": job.image_path.name,
        "output_filename": job.output_filename,
        "han_dir": str(job.han_dir),
        "viet_dir": str(job.viet_dir),
        "python_bin": python_bin,
    }
    prompt_text = render_prompt(template_text, context)
    command = build_cmd_command(
        cmd_bin=cmd_bin,
        prompt_text=prompt_text,
        permission_mode=permission_mode,
        max_turns=max_turns,
        model=model,
        effort=effort,
    )

    if dry_run:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dataset": job.dataset,
            "image": job.image_path.name,
            "status": "dry_run",
            "elapsed_s": 0.0,
            "command_preview": [c if c != prompt_text else "<RENDERED_PROMPT>" for c in command],
            "prompt": prompt_text,
        }

    job.han_dir.mkdir(parents=True, exist_ok=True)
    job.viet_dir.mkdir(parents=True, exist_ok=True)

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
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dataset": job.dataset,
            "image": job.image_path.name,
            "status": "timeout",
            "elapsed_s": round(time.monotonic() - start, 2),
            "error": f"Command timed out after {timeout}s",
        }

    elapsed = round(time.monotonic() - start, 2)
    han_exists = han_target.exists()
    viet_exists = viet_target.exists()

    if proc.returncode != 0:
        status = "error"
        error_msg = (proc.stderr or proc.stdout or "").strip()[-1000:]
    elif not han_exists and not viet_exists:
        status = "error"
        error_msg = "Agent exited with code 0 but wrote neither han nor viet output file."
    else:
        status = "ok"
        error_msg = ""

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dataset": job.dataset,
        "image": job.image_path.name,
        "status": status,
        "elapsed_s": elapsed,
        "returncode": proc.returncode,
        "han_path": str(han_target) if han_exists else None,
        "viet_path": str(viet_target) if viet_exists else None,
        "stdout": proc.stdout.strip()[-1000:],
        "error": error_msg,
    }


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
        "--python-bin",
        default=sys.executable,
        help="Python executable passed into prompts for invoking source.agents.tools.",
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
        "--permission-mode",
        default="yolo",
        choices=("default", "plan", "accept-edits", "yolo", "dont-ask"),
        help="Permission mode for `cmd` (default: yolo for unattended tool execution).",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=10,
        help="Max agent turns per image (default: 10).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Per-image timeout in seconds (default: 300).",
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

    if not args.dry_run and shutil.which(args.cmd_bin) is None:
        print(f"Error: Command Code CLI '{args.cmd_bin}' not found in PATH.", file=sys.stderr)
        return 2

    jobs = collect_jobs(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        prompts_dir=args.prompts_dir,
        datasets=args.datasets,
        pages=args.pages,
        limit=args.limit,
    )
    if not jobs:
        print("No matching images found.")
        return 0

    total = len(jobs)
    counts = {"ok": 0, "skipped": 0, "error": 0, "timeout": 0, "dry_run": 0}
    workers = max(1, args.workers)

    def _execute(idx_job: Tuple[int, PageJob]) -> Tuple[int, Dict[str, Any]]:
        idx, job = idx_job
        result = run_single_job(
            job=job,
            project_root=project_root,
            cmd_bin=args.cmd_bin,
            python_bin=args.python_bin,
            permission_mode=args.permission_mode,
            max_turns=args.max_turns,
            timeout=args.timeout,
            overwrite=args.overwrite,
            model=args.model,
            effort=args.effort,
            dry_run=args.dry_run,
        )
        return idx, result

    indexed_jobs = list(enumerate(jobs, start=1))

    def _report(idx: int, result: Dict[str, Any]) -> None:
        status = result["status"]
        counts[status] = counts.get(status, 0) + 1
        if not args.dry_run:
            _append_log(args.log_file, result)
        with _LOG_LOCK:
            prefix = f"[{idx}/{total}] {result['dataset']}/{result['image']}"
            if status == "dry_run":
                print(f"{prefix} -> DRY RUN")
                print("CMD:", " ".join(result["command_preview"]))
                print("--- PROMPT ---")
                print(result["prompt"])
                print("--------------")
            elif status == "ok":
                print(
                    f"{prefix} -> OK ({result['elapsed_s']}s) "
                    f"han={bool(result['han_path'])} viet={bool(result['viet_path'])}"
                )
            elif status == "skipped":
                print(f"{prefix} -> SKIPPED (already exists)")
            else:
                print(
                    f"{prefix} -> {status.upper()} ({result['elapsed_s']}s): {result.get('error', '')}",
                    file=sys.stderr,
                )

    if workers == 1 or args.dry_run:
        for item in indexed_jobs:
            idx, result = _execute(item)
            _report(idx, result)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_execute, item) for item in indexed_jobs]
            for fut in as_completed(futures):
                idx, result = fut.result()
                _report(idx, result)

    print(
        f"Done. total={total} ok={counts['ok']} skipped={counts['skipped']} "
        f"error={counts['error']} timeout={counts['timeout']}"
    )
    return 0 if (counts["error"] == 0 and counts["timeout"] == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
