"""File I/O tools for OCR agents outputting Han-Nom and Vietnamese text files."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Union

PathLike = Union[str, Path]
Language = Literal["han", "viet"]


def _resolve_safe_path(output_dir: PathLike, filename: str) -> Path:
    if not str(output_dir).strip():
        raise ValueError("output_dir must not be empty.")
    if not filename or not filename.strip():
        raise ValueError("filename must not be empty.")

    clean_name = filename.strip()
    if clean_name in (".", "..") or Path(clean_name).name != clean_name:
        raise ValueError(f"filename must be a simple file name without path separators: {filename!r}")
    if "/" in clean_name or "\\" in clean_name:
        raise ValueError(f"filename must not contain path separators: {filename!r}")

    base_dir = Path(output_dir).expanduser().resolve()
    target = (base_dir / clean_name).resolve()
    if target.parent != base_dir:
        raise ValueError(f"Resolved path escapes output_dir: {filename!r}")
    return target


def _normalize_text(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _atomic_write_utf8(target: Path, content: str) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, target)
    except Exception:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    return target.stat().st_size


def write_text_file(
    output_dir: str,
    filename: str,
    content: str,
    overwrite: bool = True,
) -> Dict[str, Any]:
    """Write UTF-8 OCR text content to a file inside the specified directory.

    Args:
        output_dir: Target directory where the file will be saved.
        filename: Output file name (e.g. "han.txt", "viet.txt", "page1_1.txt").
        content: Text content to write.
        overwrite: Whether to overwrite an existing file. Defaults to True.

    Returns:
        Result dictionary containing status, file path, byte size, and line count.
    """
    try:
        target = _resolve_safe_path(output_dir, filename)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    if target.exists() and not overwrite:
        return {
            "ok": False,
            "error": f"File already exists and overwrite=False: {target}",
            "path": str(target),
        }

    normalized = _normalize_text(content)
    bytes_written = _atomic_write_utf8(target, normalized)
    return {
        "ok": True,
        "path": str(target),
        "bytes_written": bytes_written,
        "lines": len(normalized.splitlines()),
    }


def append_text_file(
    output_dir: str,
    filename: str,
    content: str,
    separator: str = "\n",
) -> Dict[str, Any]:
    """Append UTF-8 OCR text content to an existing file (or create it if missing).

    Args:
        output_dir: Target directory containing the file.
        filename: Output file name (e.g. "han.txt", "viet.txt").
        content: Text content to append.
        separator: Separator inserted between existing non-empty content and new content.

    Returns:
        Result dictionary containing status, file path, byte size, and line count.
    """
    try:
        target = _resolve_safe_path(output_dir, filename)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    new_part = _normalize_text(content)
    if existing and new_part:
        combined = f"{_normalize_text(existing)}{separator}{new_part}"
    else:
        combined = f"{_normalize_text(existing)}{new_part}"

    bytes_written = _atomic_write_utf8(target, combined)
    return {
        "ok": True,
        "path": str(target),
        "bytes_written": bytes_written,
        "lines": len(combined.splitlines()),
    }


def save_han_viet_files(
    han_dir: str,
    viet_dir: str,
    han_text: Optional[str] = None,
    viet_text: Optional[str] = None,
    han_filename: str = "han.txt",
    viet_filename: str = "viet.txt",
    overwrite: bool = True,
) -> Dict[str, Any]:
    """Save Han-Nom and/or Vietnamese OCR text into their respective directories.

    Pass None for either text argument to skip writing that language file.

    Args:
        han_dir: Directory for Han-Nom output files.
        viet_dir: Directory for Vietnamese output files.
        han_text: Recognized Han-Nom text, or None to skip.
        viet_text: Recognized Vietnamese text, or None to skip.
        han_filename: File name inside han_dir. Defaults to "han.txt".
        viet_filename: File name inside viet_dir. Defaults to "viet.txt".
        overwrite: Whether to overwrite existing files. Defaults to True.

    Returns:
        Result dictionary with overall status and per-language write results.
    """
    if han_text is None and viet_text is None:
        return {
            "ok": False,
            "error": "At least one of han_text or viet_text must be provided.",
        }

    results: Dict[str, Any] = {}
    all_ok = True

    if han_text is not None:
        han_result = write_text_file(
            output_dir=han_dir,
            filename=han_filename,
            content=han_text,
            overwrite=overwrite,
        )
        results["han"] = han_result
        all_ok = all_ok and han_result["ok"]

    if viet_text is not None:
        viet_result = write_text_file(
            output_dir=viet_dir,
            filename=viet_filename,
            content=viet_text,
            overwrite=overwrite,
        )
        results["viet"] = viet_result
        all_ok = all_ok and viet_result["ok"]

    return {"ok": all_ok, "files": results}


def read_text_file(output_dir: str, filename: str) -> Dict[str, Any]:
    """Read a UTF-8 OCR text file from the specified directory.

    Args:
        output_dir: Directory containing the file.
        filename: File name to read.

    Returns:
        Result dictionary containing status, file path, and text content.
    """
    try:
        target = _resolve_safe_path(output_dir, filename)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    if not target.is_file():
        return {"ok": False, "error": f"File not found: {target}", "path": str(target)}

    content = target.read_text(encoding="utf-8")
    return {
        "ok": True,
        "path": str(target),
        "content": content,
        "lines": len(content.splitlines()),
    }


def list_output_files(output_dir: str) -> Dict[str, Any]:
    """List existing files in an OCR output directory.

    Args:
        output_dir: Directory to inspect.

    Returns:
        Result dictionary containing status, directory path, and sorted file names.
    """
    if not str(output_dir).strip():
        return {"ok": False, "error": "output_dir must not be empty."}

    base_dir = Path(output_dir).expanduser().resolve()
    if not base_dir.exists():
        return {"ok": True, "directory": str(base_dir), "files": []}
    if not base_dir.is_dir():
        return {"ok": False, "error": f"Path is not a directory: {base_dir}"}

    files = sorted(p.name for p in base_dir.iterdir() if p.is_file())
    return {"ok": True, "directory": str(base_dir), "files": files}


def build_ocr_tools(
    han_dir: PathLike,
    viet_dir: PathLike,
) -> List[Callable[..., Dict[str, Any]]]:
    """Create agent tool functions bound to pre-configured Han-Nom and Vietnamese directories.

    Use this when output directories are fixed ahead of time so the agent only provides
    recognized text and file names without needing to pass directory paths.

    Args:
        han_dir: Fixed directory for Han-Nom OCR files.
        viet_dir: Fixed directory for Vietnamese OCR files.

    Returns:
        List of callable tool functions ready to register with an agent.
    """
    resolved_han_dir = str(Path(han_dir).expanduser().resolve())
    resolved_viet_dir = str(Path(viet_dir).expanduser().resolve())
    dir_map: Dict[Language, str] = {
        "han": resolved_han_dir,
        "viet": resolved_viet_dir,
    }
    default_filename: Dict[Language, str] = {
        "han": "han.txt",
        "viet": "viet.txt",
    }

    def write_han_file(
        content: str,
        filename: str = "han.txt",
        overwrite: bool = True,
    ) -> Dict[str, Any]:
        """Write Han-Nom OCR text to the pre-configured Han output directory.

        Args:
            content: Recognized Han-Nom text.
            filename: Output file name. Defaults to "han.txt".
            overwrite: Whether to overwrite an existing file. Defaults to True.
        """
        return write_text_file(resolved_han_dir, filename, content, overwrite=overwrite)

    def write_viet_file(
        content: str,
        filename: str = "viet.txt",
        overwrite: bool = True,
    ) -> Dict[str, Any]:
        """Write Vietnamese OCR text to the pre-configured Vietnamese output directory.

        Args:
            content: Recognized Vietnamese text.
            filename: Output file name. Defaults to "viet.txt".
            overwrite: Whether to overwrite an existing file. Defaults to True.
        """
        return write_text_file(resolved_viet_dir, filename, content, overwrite=overwrite)

    def save_ocr_files(
        han_text: Optional[str] = None,
        viet_text: Optional[str] = None,
        han_filename: str = "han.txt",
        viet_filename: str = "viet.txt",
        overwrite: bool = True,
    ) -> Dict[str, Any]:
        """Save Han-Nom and/or Vietnamese OCR text to their pre-configured directories.

        Pass None for either text argument if the image does not contain that language.

        Args:
            han_text: Recognized Han-Nom text, or None to skip.
            viet_text: Recognized Vietnamese text, or None to skip.
            han_filename: Output file name in the Han directory. Defaults to "han.txt".
            viet_filename: Output file name in the Vietnamese directory. Defaults to "viet.txt".
            overwrite: Whether to overwrite existing files. Defaults to True.
        """
        return save_han_viet_files(
            han_dir=resolved_han_dir,
            viet_dir=resolved_viet_dir,
            han_text=han_text,
            viet_text=viet_text,
            han_filename=han_filename,
            viet_filename=viet_filename,
            overwrite=overwrite,
        )

    def append_ocr_text(
        language: Language,
        content: str,
        filename: Optional[str] = None,
        separator: str = "\n",
    ) -> Dict[str, Any]:
        """Append OCR text to a file in the pre-configured Han or Vietnamese directory.

        Args:
            language: Target language directory ("han" or "viet").
            content: Text content to append.
            filename: Output file name. Defaults to "han.txt" or "viet.txt".
            separator: Separator inserted before appended content.
        """
        if language not in dir_map:
            return {"ok": False, "error": f"Invalid language: {language!r}. Expected 'han' or 'viet'."}
        target_name = filename or default_filename[language]
        return append_text_file(dir_map[language], target_name, content, separator=separator)

    def read_ocr_file(
        language: Language,
        filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Read an existing OCR file from the pre-configured Han or Vietnamese directory.

        Args:
            language: Target language directory ("han" or "viet").
            filename: File name to read. Defaults to "han.txt" or "viet.txt".
        """
        if language not in dir_map:
            return {"ok": False, "error": f"Invalid language: {language!r}. Expected 'han' or 'viet'."}
        target_name = filename or default_filename[language]
        return read_text_file(dir_map[language], target_name)

    def list_ocr_files(language: Language) -> Dict[str, Any]:
        """List files in the pre-configured Han or Vietnamese output directory.

        Args:
            language: Target language directory ("han" or "viet").
        """
        if language not in dir_map:
            return {"ok": False, "error": f"Invalid language: {language!r}. Expected 'han' or 'viet'."}
        return list_output_files(dir_map[language])

    return [
        write_han_file,
        write_viet_file,
        save_ocr_files,
        append_ocr_text,
        read_ocr_file,
        list_ocr_files,
    ]


def _cli_main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m source.agents.tools",
        description="CLI wrapper for OCR agent file tools.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_save = sub.add_parser("save", help="Save Han-Nom and/or Vietnamese OCR text files.")
    p_save.add_argument("--han-dir", required=True)
    p_save.add_argument("--viet-dir", required=True)
    p_save.add_argument("--han-filename", default="han.txt")
    p_save.add_argument("--viet-filename", default="viet.txt")
    p_save.add_argument("--han-text", default=None)
    p_save.add_argument("--viet-text", default=None)
    p_save.add_argument("--no-overwrite", action="store_true")
    p_save.add_argument(
        "--stdin-json",
        action="store_true",
        help="Read JSON payload from stdin with keys: han_text, viet_text, han_filename, viet_filename.",
    )

    p_read = sub.add_parser("read", help="Read an OCR text file.")
    p_read.add_argument("--dir", required=True, dest="output_dir")
    p_read.add_argument("--filename", required=True)

    p_list = sub.add_parser("list", help="List files in an OCR output directory.")
    p_list.add_argument("--dir", required=True, dest="output_dir")

    args = parser.parse_args(argv)

    if args.command == "save":
        han_text = args.han_text
        viet_text = args.viet_text
        han_filename = args.han_filename
        viet_filename = args.viet_filename
        if args.stdin_json:
            payload = json.loads(sys.stdin.read())
            if "han_text" in payload:
                han_text = payload["han_text"]
            if "viet_text" in payload:
                viet_text = payload["viet_text"]
            if payload.get("han_filename"):
                han_filename = payload["han_filename"]
            if payload.get("viet_filename"):
                viet_filename = payload["viet_filename"]

        result = save_han_viet_files(
            han_dir=args.han_dir,
            viet_dir=args.viet_dir,
            han_text=han_text,
            viet_text=viet_text,
            han_filename=han_filename,
            viet_filename=viet_filename,
            overwrite=not args.no_overwrite,
        )
    elif args.command == "read":
        result = read_text_file(args.output_dir, args.filename)
    else:
        result = list_output_files(args.output_dir)

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_cli_main())
