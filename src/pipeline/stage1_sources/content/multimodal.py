#!/usr/bin/env python3
"""Prepare bounded, derived inspection aids without replacing scientific data.

Adapted from NatureBench's harbor/adapter/src/naturebench_adapter/multimodal_data.py.
This is a standalone stdlib orchestrator: optional decoders are imported lazily.
Image previews do not give a text-only model vision, and sampled descriptions are
never a substitute for the original inputs, labels, or official evaluation set.
Call this only on an authorized visibility scope (solver public data in stage3).
"""
from __future__ import annotations

import argparse
import bz2
from collections import Counter
import csv
import gzip
import hashlib
import importlib
import json
import lzma
import math
import mimetypes
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tarfile
from typing import Any
import wave
import zipfile


SCHEMA_VERSION = "naturebench-qwen-data-v2"
MAX_TEXT_BYTES = 64 * 1024
MAX_DECODED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10000
MAX_IMAGE_PIXELS = 64 * 1024 * 1024
TEXT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".csv", ".tsv", ".json", ".jsonl", ".yaml",
    ".yml", ".xml", ".html", ".htm", ".fa", ".fasta", ".fastq", ".bed",
    ".gff", ".gff3", ".pdb", ".meme", ".log", ".ini", ".toml", ".py",
    ".r", ".sh", ".sql",
}
TABLE_EXTENSIONS = {".csv", ".tsv", ".parquet", ".feather", ".xlsx", ".xls"}
IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff",
    ".svs", ".ndpi", ".czi",
}
VOLUME_EXTENSIONS = {".dcm", ".nii", ".nrrd", ".mha", ".mhd"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
AUDIO_EXTENSIONS = {".wav", ".flac", ".mp3", ".ogg", ".m4a"}
ARRAY_EXTENSIONS = {".npy", ".npz", ".h5", ".hdf5", ".h5ad", ".zarr"}
SERIALIZED_EXTENSIONS = {".pt", ".pth", ".pkl", ".pickle", ".joblib"}
GENOMIC_EXTENSIONS = {".bw", ".bigwig"}
ARCHIVE_EXTENSIONS = {".zip", ".tar", ".tgz", ".tbz", ".tbz2", ".gz", ".bz2", ".xz"}
QWEN_NATIVE_IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _decoder_hints(extension: str) -> list[str]:
    if extension in IMAGE_EXTENSIONS:
        return ["Pillow"] + (["tifffile", "imagecodecs (compressed scientific TIFF)"] if extension in {".tif", ".tiff", ".svs", ".ndpi"} else [])
    return {
        ".pdf": ["PyMuPDF OR poppler-utils (pdftotext, pdftoppm)", "OCR for scanned text when required"],
        ".npy": ["numpy", "Pillow (optional array preview)"],
        ".npz": ["numpy", "Pillow (optional array preview)"],
        ".dcm": ["pydicom", "numpy", "Pillow", "pixel codec for compressed DICOM"],
        ".nii": ["nibabel", "numpy", "Pillow"],
        ".h5": ["h5py"], ".hdf5": ["h5py"], ".h5ad": ["h5py"], ".zarr": ["zarr"],
        ".parquet": ["pyarrow"], ".feather": ["pyarrow"], ".xlsx": ["openpyxl"],
        ".bw": ["pyBigWig"], ".bigwig": ["pyBigWig"],
    }.get(extension, ["ffmpeg / ffprobe"] if extension in VIDEO_EXTENSIONS | (AUDIO_EXTENSIONS - {".wav"}) else [])


def _import(name: str):
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _suffix(path: Path) -> str:
    lower = path.name.lower()
    if lower.endswith(".nii.gz"):
        return ".nii"
    if lower.endswith(".tar.gz"):
        return ".tgz"
    if lower.endswith(".tar.bz2"):
        return ".tbz2"
    return path.suffix.lower()


def _safe_member(name: str) -> str | None:
    path = PurePosixPath(name)
    if not name or "\\" in name or "\x00" in name or path.is_absolute() or ".." in path.parts:
        return None
    normalized = path.as_posix()
    if normalized != name or any(part in {"", "."} for part in path.parts):
        return None
    return normalized


def _preview_name(relative: str, suffix: str = ".png") -> str:
    digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:12]
    stem = Path(relative).name.replace(" ", "_")[:80]
    return f"{stem}-{digest}{suffix}"


def _json_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "item"):
        try:
            return _json_value(value.item())
        except (TypeError, ValueError):
            pass
    return str(value)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    return _json_value(value)


def _array_summary(array: Any) -> dict[str, Any]:
    numpy = _import("numpy")
    if numpy is None:
        return {"error": "numpy is not installed"}
    value = numpy.asarray(array)
    summary: dict[str, Any] = {
        "shape": list(value.shape),
        "dtype": str(value.dtype),
        "elements": int(value.size),
    }
    if value.size and numpy.issubdtype(value.dtype, numpy.number):
        flat = value.reshape(-1)
        if flat.size > 1_000_000:
            flat = flat[:: math.ceil(flat.size / 1_000_000)]
        finite = flat[numpy.isfinite(flat)]
        if finite.size:
            summary.update({
                "min": _json_value(finite.min()),
                "max": _json_value(finite.max()),
                "mean": _json_value(finite.mean()),
                "p01": _json_value(numpy.percentile(finite, 1)),
                "p99": _json_value(numpy.percentile(finite, 99)),
            })
    return summary


def _array_plane(array: Any) -> Any:
    numpy = _import("numpy")
    if numpy is None:
        raise RuntimeError("numpy is not installed")
    value = numpy.asarray(array)
    value = numpy.squeeze(value)
    while value.ndim > 3:
        value = value[value.shape[0] // 2]
    if value.ndim == 3 and value.shape[-1] not in {3, 4}:
        value = value[value.shape[0] // 2]
    if value.ndim not in {2, 3}:
        raise ValueError(f"array is not image-like after slicing: shape={value.shape}")
    return value


def _save_array_preview(array: Any, target: Path) -> None:
    numpy = _import("numpy")
    pil = _import("PIL.Image")
    if numpy is None or pil is None:
        raise RuntimeError("numpy and Pillow are required for image previews")
    value = _array_plane(array)
    if value.nbytes > MAX_DECODED_BYTES or value.size > MAX_IMAGE_PIXELS:
        raise ValueError("image plane exceeds bounded preview memory limit")
    if value.ndim == 3 and value.shape[-1] in {3, 4}:
        if value.dtype != numpy.uint8:
            numeric = value.astype("float32")
            finite = numeric[numpy.isfinite(numeric)]
            low, high = (0.0, 1.0) if not finite.size else (
                float(numpy.percentile(finite, 1)),
                float(numpy.percentile(finite, 99)),
            )
            numeric = numpy.clip((numeric - low) / max(high - low, 1e-12), 0, 1)
            value = (numeric * 255).astype("uint8")
    else:
        numeric = value.astype("float32")
        finite = numeric[numpy.isfinite(numeric)]
        low, high = (0.0, 1.0) if not finite.size else (
            float(numpy.percentile(finite, 1)),
            float(numpy.percentile(finite, 99)),
        )
        numeric = numpy.nan_to_num(numeric, nan=low, posinf=high, neginf=low)
        value = (numpy.clip((numeric - low) / max(high - low, 1e-12), 0, 1) * 255).astype("uint8")
    image = pil.fromarray(value)
    image.thumbnail((1024, 1024))
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format="PNG")


def _describe_text(path: Path, delimiter: str | None = None) -> dict[str, Any]:
    with path.open("rb") as stream:
        sample = stream.read(MAX_TEXT_BYTES)
    text = sample.decode("utf-8", errors="replace")
    truncated = path.stat().st_size > len(sample)
    result: dict[str, Any] = {
        "sample_characters": len(text),
        "sample_lines": text.count("\n") + bool(text),
        "sample_bytes": len(sample),
        "truncated": truncated,
        "excerpt": text[:4000],
    }
    if delimiter:
        rows = list(csv.reader(text.splitlines()[:25], delimiter=delimiter))
        if rows:
            result["columns"] = rows[0]
            result["sample_rows"] = rows[1:6]
    elif path.suffix.lower() == ".json" and not truncated:
        try:
            value = json.loads(text)
            result["json_type"] = type(value).__name__
            if isinstance(value, dict):
                result["top_level_keys"] = list(value)[:100]
            elif isinstance(value, list):
                result["items"] = len(value)
        except json.JSONDecodeError as error:
            result["parse_error"] = str(error)
    return result


def _describe_table(path: Path) -> tuple[str, dict[str, Any]]:
    extension = _suffix(path)
    if extension in {".csv", ".tsv"}:
        return "described", _describe_text(path, "\t" if extension == ".tsv" else ",")
    if extension == ".parquet":
        parquet = _import("pyarrow.parquet")
        if parquet is None:
            return "unsupported", {"reason": "pyarrow is not installed"}
        dataset = parquet.ParquetFile(path)
        batches = dataset.iter_batches(batch_size=5)
        first = next(batches, None)
        return "described", {
            "rows": dataset.metadata.num_rows,
            "row_groups": dataset.metadata.num_row_groups,
            "columns": dataset.schema_arrow.names,
            "schema": str(dataset.schema_arrow),
            "sample_rows": first.to_pylist() if first is not None else [],
        }
    if extension == ".feather":
        feather = _import("pyarrow.feather")
        if feather is None:
            return "unsupported", {"reason": "pyarrow is not installed"}
        table = feather.read_table(path, memory_map=True)
        return "described", {
            "rows": table.num_rows,
            "columns": table.column_names,
            "schema": str(table.schema),
            "sample_rows": table.slice(0, 5).to_pylist(),
        }
    if extension in {".xlsx", ".xls"}:
        openpyxl = _import("openpyxl")
        if openpyxl is None or extension == ".xls":
            return "unsupported", {"reason": "openpyxl is required for XLSX; legacy XLS is unsupported"}
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            sheets = []
            for sheet in workbook.worksheets[:20]:
                rows = []
                for index, row in enumerate(sheet.iter_rows(values_only=True)):
                    if index >= 6:
                        break
                    rows.append([_json_value(value) for value in row[:100]])
                sheets.append({
                    "title": sheet.title,
                    "max_row": sheet.max_row,
                    "max_column": sheet.max_column,
                    "sample_rows": rows,
                })
            return "described", {"sheets": sheets, "sheet_count": len(workbook.sheetnames)}
        finally:
            workbook.close()
    return "unsupported", {"reason": f"no table decoder for {extension}"}


def _describe_serialized(path: Path) -> tuple[str, dict[str, Any]]:
    details: dict[str, Any] = {
        "format": _suffix(path),
        "safety": "metadata only; serialized objects were not executed or unpickled",
    }
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            details["container"] = "zip"
            details["members"] = [
                {"path": item.filename, "bytes": item.file_size}
                for item in members[:500]
                if not item.is_dir()
            ]
            details["member_count"] = len(members)
            details["truncated"] = len(members) > 500
    else:
        with path.open("rb") as stream:
            details["header_hex"] = stream.read(64).hex()
    return "described", details


def _describe_bigwig(path: Path) -> tuple[str, dict[str, Any]]:
    pybigwig = _import("pyBigWig")
    if pybigwig is None:
        return "unsupported", {"reason": "pyBigWig is not installed"}
    handle = pybigwig.open(str(path))
    try:
        return "described", {
            "header": handle.header(),
            "chromosomes": dict(list(handle.chroms().items())[:500]),
        }
    finally:
        handle.close()


def _describe_unknown(path: Path) -> tuple[str, dict[str, Any]]:
    with path.open("rb") as stream:
        sample = stream.read(256 * 1024)
    if not sample:
        return "described", {"detected_text": True, "characters": 0, "excerpt": ""}
    if b"\x00" in sample:
        return "unsupported", {"reason": "unknown binary format containing NUL bytes"}
    text = sample.decode("utf-8", errors="replace")
    replacement_ratio = text.count("\ufffd") / max(len(text), 1)
    control = sum(ord(char) < 32 and char not in "\n\r\t" for char in text)
    control_ratio = control / max(len(text), 1)
    if replacement_ratio <= 0.01 and control_ratio <= 0.01:
        return "described", {
            "detected_text": True,
            "sample_bytes": len(sample),
            "excerpt": text[:4000],
        }
    return "unsupported", {
        "reason": "unknown binary format",
        "sample_bytes": len(sample),
        "utf8_replacement_ratio": replacement_ratio,
        "control_character_ratio": control_ratio,
    }


def _describe_image(path: Path, preview: Path) -> tuple[str, dict[str, Any]]:
    extension = _suffix(path)
    if extension in QWEN_NATIVE_IMAGES:
        pil = _import("PIL.Image")
        if pil is None:
            return "unsupported", {"reason": "Pillow is required to validate native images"}
        metadata: dict[str, Any] = {"native_image_format": True}
        with pil.open(path) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("image dimensions exceed bounded preview pixel limit")
            metadata.update({"width": image.width, "height": image.height, "mode": image.mode})
            image.verify()
        return "ready", metadata

    decoder_errors: list[str] = []
    if extension in {".tif", ".tiff", ".svs", ".ndpi"}:
        tifffile = _import("tifffile")
        if tifffile is not None:
            try:
                with tifffile.TiffFile(path) as handle:
                    series = handle.series[0]
                    level = series.levels[-1] if getattr(series, "levels", None) else series
                    if math.prod(level.shape) * level.dtype.itemsize > MAX_DECODED_BYTES:
                        raise ValueError("TIFF preview level exceeds bounded decoded memory limit")
                    array = level.asarray()
                    metadata = {
                        "decoder": "tifffile",
                        "series_count": len(handle.series),
                        "shape": list(series.shape),
                        "dtype": str(series.dtype),
                        "preview_level_shape": list(array.shape),
                    }
                _save_array_preview(array, preview)
                return "converted", metadata
            except Exception as error:
                decoder_errors.append(f"tifffile: {type(error).__name__}: {error}")

    pil = _import("PIL.Image")
    if pil is None:
        reason = "; ".join(decoder_errors) or "Pillow/tifffile decoder is not installed"
        return "unsupported", {"reason": reason}
    try:
        with pil.open(path) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("image dimensions exceed bounded preview pixel limit")
            metadata = {
                "decoder": "Pillow",
                "width": image.width,
                "height": image.height,
                "mode": image.mode,
            }
            numpy = _import("numpy")
            if numpy is not None:
                _save_array_preview(numpy.asarray(image), preview)
                metadata["preview_normalization"] = "p01-p99"
            else:
                converted = image.convert("RGB")
                converted.thumbnail((1024, 1024))
                preview.parent.mkdir(parents=True, exist_ok=True)
                converted.save(preview, format="PNG")
                metadata["preview_normalization"] = "none"
        if decoder_errors:
            metadata["decoder_fallbacks"] = decoder_errors
        return "converted", metadata
    except Exception as error:
        decoder_errors.append(f"Pillow: {type(error).__name__}: {error}")
        return "unsupported", {"reason": "; ".join(decoder_errors)}


def _describe_numpy(path: Path, preview: Path) -> tuple[str, dict[str, Any]]:
    numpy = _import("numpy")
    if numpy is None:
        return "unsupported", {"reason": "numpy is not installed"}
    if path.suffix.lower() == ".npz":
        with zipfile.ZipFile(path) as inventory:
            if sum(item.file_size for item in inventory.infolist()) > MAX_DECODED_BYTES:
                return "unsupported", {"reason": "NPZ decoded arrays exceed bounded memory limit"}
        with numpy.load(path, allow_pickle=False) as archive:
            arrays = {name: _array_summary(archive[name]) for name in archive.files[:100]}
            first = archive[archive.files[0]] if archive.files else None
            if first is not None:
                try:
                    _save_array_preview(first, preview)
                    return "converted", {"arrays": arrays}
                except (ValueError, RuntimeError):
                    pass
            return "described", {"arrays": arrays}
    value = numpy.load(path, mmap_mode="r", allow_pickle=False)
    summary = _array_summary(value)
    try:
        _save_array_preview(value, preview)
        return "converted", summary
    except (ValueError, RuntimeError):
        return "described", summary


def _describe_hdf5(path: Path) -> tuple[str, dict[str, Any]]:
    h5py = _import("h5py")
    if h5py is None:
        return "unsupported", {"reason": "h5py is not installed"}
    datasets: list[dict[str, Any]] = []
    with h5py.File(path, "r") as handle:
        def visit(name: str, value: Any):
            if len(datasets) >= 200:
                return True
            if hasattr(value, "shape") and hasattr(value, "dtype"):
                datasets.append({"path": name, "shape": list(value.shape), "dtype": str(value.dtype)})
        handle.visititems(visit)
    return "described", {"datasets": datasets, "truncated": len(datasets) == 200}


def _describe_zarr(path: Path) -> tuple[str, dict[str, Any]]:
    zarr = _import("zarr")
    if zarr is None:
        return "unsupported", {"reason": "zarr is not installed"}
    root = zarr.open(str(path), mode="r")
    arrays: list[dict[str, Any]] = []

    def visit(name: str, value: Any) -> None:
        if len(arrays) >= 200:
            return
        if hasattr(value, "shape") and hasattr(value, "dtype"):
            arrays.append({
                "path": name,
                "shape": list(value.shape),
                "dtype": str(value.dtype),
                "chunks": list(value.chunks) if value.chunks else None,
            })

    if hasattr(root, "visititems"):
        root.visititems(visit)
    elif hasattr(root, "shape"):
        visit("/", root)
    else:
        return "unsupported", {"reason": "installed Zarr group API has no supported bounded inventory method"}
    return "described", {"arrays": arrays, "truncated": len(arrays) == 200}


def _describe_dicom(path: Path, preview: Path) -> tuple[str, dict[str, Any]]:
    pydicom = _import("pydicom")
    if pydicom is None:
        return "unsupported", {"reason": "pydicom is not installed"}
    header = pydicom.dcmread(path, stop_before_pixels=True)
    decoded_bytes = (
        int(getattr(header, "Rows", 0) or 0) * int(getattr(header, "Columns", 0) or 0)
        * int(getattr(header, "NumberOfFrames", 1) or 1)
        * int(getattr(header, "SamplesPerPixel", 1) or 1)
        * max(1, int(getattr(header, "BitsAllocated", 8) or 8) // 8)
    )
    if decoded_bytes > MAX_DECODED_BYTES:
        raise ValueError("DICOM pixels exceed bounded decoded memory limit")
    dataset = pydicom.dcmread(path, stop_before_pixels=False)
    metadata = {
        "modality": str(getattr(dataset, "Modality", "")),
        "rows": int(getattr(dataset, "Rows", 0) or 0),
        "columns": int(getattr(dataset, "Columns", 0) or 0),
        "frames": int(getattr(dataset, "NumberOfFrames", 1) or 1),
    }
    _save_array_preview(dataset.pixel_array, preview)
    return "converted", metadata


def _describe_nifti(path: Path, preview: Path) -> tuple[str, dict[str, Any]]:
    nibabel = _import("nibabel")
    if nibabel is None:
        return "unsupported", {"reason": "nibabel is not installed"}
    image = nibabel.load(str(path))
    shape = list(image.shape)
    selection = tuple(slice(None) if i < 2 else size // 2 for i, size in enumerate(shape))
    if math.prod(shape[:2]) * image.get_data_dtype().itemsize > MAX_DECODED_BYTES:
        raise ValueError("NIfTI plane exceeds bounded decoded memory limit")
    plane = image.dataobj[selection]
    _save_array_preview(plane, preview)
    return "converted", {"shape": shape, "dtype": str(image.get_data_dtype())}


def _describe_audio(path: Path, preview: Path) -> tuple[str, dict[str, Any]]:
    if path.suffix.lower() != ".wav":
        if not shutil.which("ffprobe"):
            return "unsupported", {"reason": "ffprobe is required for non-WAV audio metadata"}
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name,sample_rate,channels",
            "-of", "json", str(path),
        ], capture_output=True, text=True, timeout=45)
        if result.returncode:
            return "unsupported", {"reason": result.stderr[-1000:]}
        metadata = json.loads(result.stdout)
        metadata["limitation"] = "metadata only; no speech transcription or audio understanding"
        return "described", metadata
    with wave.open(str(path), "rb") as audio:
        frames = audio.getnframes()
        rate = audio.getframerate()
        metadata = {
            "channels": audio.getnchannels(),
            "sample_width": audio.getsampwidth(),
            "sample_rate": rate,
            "frames": frames,
            "duration_seconds": frames / rate if rate else None,
            "limitation": "metadata only; no speech transcription or audio understanding",
        }
    return "described", metadata


def _describe_video(path: Path, preview: Path) -> tuple[str, dict[str, Any]]:
    if shutil.which("ffmpeg") is None:
        return "unsupported", {"reason": "ffmpeg is not installed"}
    preview.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-threads", "1", "-ss", "0", "-i", str(path),
         "-frames:v", "1", "-vf", "scale=1024:1024:force_original_aspect_ratio=decrease", str(preview)],
        capture_output=True,
        text=True,
        timeout=45,
    )
    if process.returncode != 0 or not preview.is_file():
        return "unsupported", {"reason": process.stderr[-1000:]}
    return "converted", {"preview_second": 0, "limitation": "one sampled frame; not full video interpretation"}


def _describe_pdf(path: Path, preview: Path) -> tuple[str, dict[str, Any]]:
    details: dict[str, Any] = {
        "limitation": "text from first 5 pages and first-page preview; original PDF remains authoritative",
    }
    fitz = _import("fitz")
    if fitz is not None:
        with fitz.open(path) as document:
            details["pages"] = len(document)
            details["text_excerpt"] = "\n".join(
                document[index].get_text()[:MAX_TEXT_BYTES // 5]
                for index in range(min(len(document), 5))
            )[:MAX_TEXT_BYTES]
            if len(document):
                page = document[0]
                scale = 1024 / max(page.rect.width, page.rect.height, 1)
                preview.parent.mkdir(parents=True, exist_ok=True)
                page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).save(str(preview))
    else:
        if shutil.which("pdftotext"):
            result = subprocess.run(
                ["pdftotext", "-f", "1", "-l", "5", "-enc", "UTF-8", str(path), "-"],
                capture_output=True, timeout=45,
            )
            if result.returncode == 0:
                details["text_excerpt"] = result.stdout[:MAX_TEXT_BYTES].decode("utf-8", errors="replace")
            else:
                details["text_error"] = result.stderr[-1000:].decode("utf-8", errors="replace")
        if shutil.which("pdftoppm"):
            preview.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                ["pdftoppm", "-f", "1", "-singlefile", "-png", "-scale-to", "1024", str(path), str(preview.with_suffix(""))],
                capture_output=True, timeout=45,
            )
            if result.returncode:
                details["preview_error"] = result.stderr[-1000:].decode("utf-8", errors="replace")
    if preview.is_file():
        details["text_available"] = bool(details.get("text_excerpt", "").strip())
        return "converted", details
    if details.get("text_excerpt", "").strip():
        details["visual_preview_available"] = False
        return "described", details
    details["reason"] = "PDF has no decoded text/preview; install PyMuPDF or Poppler in the task environment; scanned pages may need OCR"
    return "unsupported", details


def _archive_inventory(
    path: Path,
    sample_root: Path,
    max_auxiliary_samples: int,
    max_sample_bytes: int,
    max_total_bytes: int,
) -> tuple[str, dict[str, Any], list[Path]]:
    counts: Counter[str] = Counter()
    extracted: list[Path] = []
    skipped_multimodal: list[dict[str, Any]] = []
    auxiliary_samples = 0
    extracted_bytes = 0

    multimodal_categories = {
        "image", "volume", "video", "audio", "array", "serialized", "genomic", "document",
    }

    def eligible(name: str, size: int) -> bool:
        nonlocal auxiliary_samples, extracted_bytes
        extension = _suffix(Path(name))
        category = _category(extension)
        if category == "archive":
            skipped_multimodal.append({
                "path": name,
                "bytes": size,
                "reason": "nested archive requires explicit extraction",
            })
            return False
        if category in multimodal_categories:
            if len(extracted) >= max_auxiliary_samples:
                skipped_multimodal.append({"path": name, "reason": "archive sample count limit reached"})
                return False
            if size > max_sample_bytes:
                skipped_multimodal.append({
                    "path": name,
                    "bytes": size,
                    "reason": f"member exceeds {max_sample_bytes}-byte conversion limit",
                })
                return False
            if extracted_bytes + size > max_total_bytes:
                skipped_multimodal.append({
                    "path": name,
                    "bytes": size,
                    "reason": f"archive conversion exceeds {max_total_bytes}-byte total limit",
                })
                return False
            extracted_bytes += size
            return True
        if category in {"text", "table"} and auxiliary_samples < max_auxiliary_samples:
            if size <= max_sample_bytes and extracted_bytes + size <= max_total_bytes:
                auxiliary_samples += 1
                extracted_bytes += size
                return True
        return False

    def target_for(name: str) -> Path:
        digest = hashlib.sha256(f"{path}:{name}".encode("utf-8")).hexdigest()[:12]
        return sample_root / digest / Path(name).name

    extension = _suffix(path)
    if extension in {".gz", ".bz2", ".xz"}:
        openers = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}
        name = path.name[: -len(extension)]
        target = target_for(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with openers[extension](path, "rb") as source, target.open("wb") as output:
                while True:
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    written += len(block)
                    if written > max_sample_bytes or written > max_total_bytes:
                        raise ValueError("decompressed member exceeds conversion limit")
                    output.write(block)
        except Exception as error:
            target.unlink(missing_ok=True)
            details = {
                "members": 1,
                "suffix_counts": {_suffix(Path(name)) or "<none>": 1},
                "extracted_bytes": written,
                "skipped_multimodal": [{"path": name, "reason": str(error)}],
            }
            return "unsupported", details, []
        extracted.append(target)
        details = {
            "members": 1,
            "suffix_counts": {_suffix(Path(name)) or "<none>": 1},
            "extracted_bytes": written,
            "skipped_multimodal": [],
        }
        return "described", details, extracted

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                return "unsupported", {"reason": "archive member count exceeds bounded inventory limit"}, []
            for member in members:
                safe = _safe_member(member.filename)
                if member.is_dir():
                    continue
                if safe is None or stat.S_ISLNK(member.external_attr >> 16):
                    skipped_multimodal.append({"path": member.filename, "reason": "unsafe archive path or link"})
                    continue
                counts[_suffix(Path(safe)) or "<none>"] += 1
                if eligible(safe, member.file_size):
                    target = target_for(safe)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                    extracted.append(target)
            details = {
                "members": len(members),
                "suffix_counts": dict(counts),
                "extracted_bytes": extracted_bytes,
                "skipped_multimodal": skipped_multimodal,
            }
            return ("unsupported" if skipped_multimodal else "described"), details, extracted

    with tarfile.open(path, "r|*") as archive:
        member_count = 0
        for member in archive:
            member_count += 1
            if member_count > MAX_ARCHIVE_MEMBERS:
                skipped_multimodal.append({"reason": "archive member count exceeds bounded inventory limit"})
                break
            safe = _safe_member(member.name)
            if member.isdir():
                continue
            if safe is None or not member.isfile():
                skipped_multimodal.append({"path": member.name, "reason": "unsafe archive path or link"})
                continue
            counts[_suffix(Path(safe)) or "<none>"] += 1
            if eligible(safe, member.size):
                source = archive.extractfile(member)
                if source is None:
                    continue
                target = target_for(safe)
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                extracted.append(target)
        details = {
            "members": member_count,
            "suffix_counts": dict(counts),
            "extracted_bytes": extracted_bytes,
            "skipped_multimodal": skipped_multimodal,
        }
        return ("unsupported" if skipped_multimodal else "described"), details, extracted


def _category(extension: str) -> str:
    if extension in TABLE_EXTENSIONS:
        return "table"
    if extension in TEXT_EXTENSIONS:
        return "text"
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in VOLUME_EXTENSIONS:
        return "volume"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    if extension in ARRAY_EXTENSIONS:
        return "array"
    if extension in SERIALIZED_EXTENSIONS:
        return "serialized"
    if extension in GENOMIC_EXTENSIONS:
        return "genomic"
    if extension in ARCHIVE_EXTENSIONS:
        return "archive"
    if extension == ".pdf":
        return "document"
    return "binary"


def describe_file(path: Path, data_root: Path, preview_root: Path) -> dict[str, Any]:
    relative = _relative(path, data_root)
    extension = _suffix(path)
    category = _category(extension)
    preview = preview_root / _preview_name(relative)
    preview.unlink(missing_ok=True)
    record: dict[str, Any] = {
        "path": relative,
        "bytes": path.stat().st_size,
        "extension": extension,
        "mime_type": mimetypes.guess_type(path.name)[0],
        "category": category,
    }
    try:
        if category == "text":
            status, details = "described", _describe_text(path)
        elif category == "table":
            status, details = _describe_table(path)
        elif category == "image":
            status, details = _describe_image(path, preview)
        elif extension in {".npy", ".npz"}:
            status, details = _describe_numpy(path, preview)
        elif extension in {".h5", ".hdf5", ".h5ad"}:
            status, details = _describe_hdf5(path)
        elif category == "serialized":
            status, details = _describe_serialized(path)
        elif category == "genomic":
            status, details = _describe_bigwig(path)
        elif extension == ".dcm":
            status, details = _describe_dicom(path, preview)
        elif extension == ".nii":
            status, details = _describe_nifti(path, preview)
        elif category == "audio":
            status, details = _describe_audio(path, preview)
        elif category == "video":
            status, details = _describe_video(path, preview)
        elif category == "document":
            status, details = _describe_pdf(path, preview)
        elif category == "binary":
            status, details = _describe_unknown(path)
            if status == "described" and details.get("detected_text"):
                category = "text"
                record["category"] = category
        else:
            status, details = "unsupported", {"reason": f"no decoder for {extension or 'extensionless file'}"}
    except Exception as error:
        status, details = "unsupported", {"reason": f"{type(error).__name__}: {error}"}
    record["status"] = status
    record["details"] = _json_safe(details)
    if category == "text" or extension in {".csv", ".tsv"}:
        record["qwen_representation"] = relative
    elif status == "ready":
        record["qwen_representation"] = relative
    elif status == "converted" and preview.is_file():
        record["qwen_representation"] = _relative(preview, data_root)
    return record


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Data inspection overview", "",
        f"Files inventoried: {report['summary']['file_count']}",
        f"Unsupported: {report['summary']['unsupported_count']}",
        f"Declared model capability: {report['compatibility']['model_capability']}", "",
        "Read qwen_text_representation with text tools. image_preview requires a vision-capable model/tool.",
        "These are sampled, derived inspection aids. Use ORIGINAL files for all computation and scoring.",
        "Metadata is not transcription, OCR, image semantics, or a replacement dataset.", "",
    ]
    for item in report["files"][:300]:
        lines.append(f"- `{item['path']}`: {item['category']}, {item['status']}; text: `{item.get('qwen_text_representation', '')}`")
        if item.get("image_preview"):
            lines.append(f"  Visual preview: `{item['image_preview']}`")
        if item.get("details", {}).get("reason"):
            lines.append(f"  Reason: {item['details']['reason']}")
    if len(report["files"]) > 300:
        lines.append("Full inventory continues in DATA_OVERVIEW.json.")
    return "\n".join(lines) + "\n"


def _walk_sources(data_root: Path, output_root: Path, max_files: int):
    """Never follow symlinks, including directory links; bound traversal entries."""
    found: list[Path] = []
    truncated = False
    for directory, dirs, files in os.walk(data_root, followlinks=False):
        base = Path(directory)
        retained = []
        names = list(files)
        for name in sorted(dirs):
            path = base / name
            if path == output_root:
                continue
            if path.is_symlink() or path.suffix.lower() == ".zarr":
                names.append(name)
            else:
                retained.append(name)
        dirs[:] = retained
        for name in sorted(names):
            if name in {"READY.json", ".naturebench-download.lock", "PROVISIONING.json", "PROVISIONING.json.tmp", ".provisioning.lock"}:
                continue
            if len(found) >= max_files:
                return found, True
            found.append(base / name)
    return found, truncated


def _reset_owned_output(data_root: Path, output_root: Path) -> None:
    """Remove stale derived aids only from a previously owned generated output."""
    if not output_root.exists() or not any(output_root.iterdir()):
        return
    report_path = output_root / "DATA_OVERVIEW.json"
    if not report_path.is_file():
        raise ValueError("nonempty output has no converter ownership report; use a fresh output directory")
    previous = json.loads(report_path.read_text(encoding="utf-8"))
    if previous.get("schema_version") != SCHEMA_VERSION or previous.get("data_root") != str(data_root):
        raise ValueError("existing output belongs to a different converter or input root; use a fresh directory")
    managed_dirs = {"qwen_descriptions", "qwen_previews", "archive_samples"}
    managed_files = {"DATA_OVERVIEW.json", "DATA_OVERVIEW.md"}
    if any(path.name not in managed_dirs | managed_files for path in output_root.iterdir()):
        raise ValueError("conversion output contains unowned files; use a fresh directory")
    for path in output_root.iterdir():
        if path.name in managed_dirs and path.is_dir():
            shutil.rmtree(path)
        elif path.name in managed_files and path.is_file():
            path.unlink()
        else:
            raise ValueError("unexpected file type in converter output")


def prepare_multimodal_data(
    data_root: Path,
    output_root: Path,
    *,
    model_capability: str = "text",
    max_files: int = 10000,
    max_file_bytes: int = 256 * 1024 * 1024,
    max_archive_samples: int = 32,
    max_archive_sample_bytes: int = 256 * 1024 * 1024,
    max_archive_total_bytes: int = 1024 * 1024 * 1024,
) -> dict[str, Any]:
    """Return DATA_OVERVIEW schema v2; unsupported formats never count as ready.

    The caller chooses visibility scope. In solver preparation use ONLY the final
    public bundle, never an authoring/source bundle that contains private labels.
    model_capability is an explicit declaration, not a model-name heuristic and
    not verification that an API endpoint actually accepts image input.
    """
    if model_capability not in {"text", "vision", "unknown"}:
        raise ValueError("model_capability must be text, vision, or unknown")
    if min(max_files, max_file_bytes, max_archive_sample_bytes, max_archive_total_bytes) <= 0 or max_archive_samples < 0:
        raise ValueError("conversion limits must be positive (sample count may be zero)")
    data_root = Path(data_root).resolve()
    requested_output = Path(output_root).absolute()
    output_root = requested_output.resolve()
    if not data_root.is_dir():
        raise FileNotFoundError(data_root)
    if output_root == data_root or not output_root.is_relative_to(data_root):
        raise ValueError("output directory must be strictly inside the data directory")
    if requested_output != output_root:
        raise ValueError("output directory must not contain symlink components")
    if output_root.exists() and any(p.is_symlink() for p in output_root.rglob("*")):
        raise ValueError("existing conversion output contains a symlink")
    _reset_owned_output(data_root, output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    preview_root = output_root / "qwen_previews"
    description_root = output_root / "qwen_descriptions"
    archive_samples = output_root / "archive_samples"
    paths, truncated = _walk_sources(data_root, output_root, max_files)
    records: list[dict[str, Any]] = []
    extracted: list[Path] = []
    original_bytes = 0
    archive_bytes = 0
    for path in paths:
        extension = _suffix(path)
        record: dict[str, Any] = {
            "path": _relative(path, data_root), "bytes": path.lstat().st_size,
            "extension": extension, "category": _category(extension),
            "mime_type": mimetypes.guess_type(path.name)[0], "original": True,
        }
        original_bytes += record["bytes"]
        if path.is_symlink() or not (path.is_file() or extension == ".zarr" and path.is_dir()):
            record.update(status="unsupported", details={"reason": "symlink or nonregular input is not inspected"})
        elif record["bytes"] > max_file_bytes:
            record.update(status="unsupported", details={"reason": f"file exceeds {max_file_bytes}-byte inspection limit"})
        elif extension == ".zarr":
            try:
                if any(p.is_symlink() for p in path.rglob("*")):
                    raise ValueError("Zarr store contains symlink")
                status, details = _describe_zarr(path)
                record.update(status=status, details=details)
            except Exception as error:
                record.update(status="unsupported", details={"reason": f"{type(error).__name__}: {error}"})
        elif extension in ARCHIVE_EXTENSIONS:
            try:
                budget = max_archive_total_bytes - archive_bytes
                if budget <= 0:
                    raise ValueError("total archive extraction byte budget exhausted")
                status, details, samples = _archive_inventory(
                    path, archive_samples, max_archive_samples,
                    min(max_archive_sample_bytes, max_file_bytes), budget,
                )
                archive_bytes += int(details.get("extracted_bytes", 0))
                record.update(status=status, details=details)
                extracted.extend(samples)
                for sample in samples:
                    child = describe_file(sample, data_root, preview_root)
                    child.update(original=False, derived_from=record["path"])
                    records.append(child)
            except Exception as error:
                record.update(status="unsupported", details={"reason": f"{type(error).__name__}: {error}"})
        else:
            record.update(describe_file(path, data_root, preview_root))
        if path.is_file() and not path.is_symlink() and record["bytes"] <= max_file_bytes:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            record["sha256"] = digest.hexdigest()
        records.append(record)
    if truncated:
        records.append({"path": ".", "bytes": 0, "category": "inventory", "status": "unsupported",
                        "details": {"reason": f"inventory exceeded {max_files}-file limit"}, "original": False})
    description_root.mkdir(parents=True, exist_ok=True)
    for record in records:
        record["decoder_hints"] = _decoder_hints(record.get("extension", ""))
        representation = record.pop("qwen_representation", None)
        if representation and record["category"] in {"image", "volume", "video", "document", "array"}:
            record["image_preview"] = representation
        target = description_root / _preview_name(record["path"], ".json")
        descriptor = {
            "source_path": record["path"], "original": record["original"],
            "sha256": record.get("sha256"), "category": record["category"],
            "status": record["status"], "details": record.get("details", {}),
            "decoder_hints": record["decoder_hints"],
            "image_preview": record.get("image_preview"),
            "notice": "Derived inspection aid. Original files are authoritative; metadata is not semantic perception.",
        }
        target.write_text(json.dumps(_json_safe(descriptor), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        record["qwen_text_representation"] = _relative(target, data_root)
        record["qwen_representation"] = (
            record["image_preview"] if model_capability == "vision" and record.get("image_preview")
            else record["qwen_text_representation"]
        )
    statuses = Counter(item["status"] for item in records)
    visual_count = sum(bool(item.get("image_preview")) for item in records)
    report = {
        "schema_version": SCHEMA_VERSION,
        "data_root": str(data_root), "output_root": str(output_root),
        "summary": {
            "file_count": len(records), "original_file_count": len(paths),
            "archive_sample_count": len(extracted), "total_bytes": original_bytes,
            "unsupported_count": statuses.get("unsupported", 0), "inventory_truncated": truncated,
            "statuses": dict(statuses), "categories": dict(Counter(item["category"] for item in records)),
        },
        "compatibility": {
            "model_capability": model_capability, "model_capability_verified": False,
            "ready_for_inspection": not statuses.get("unsupported", 0),
            "text_descriptors_available": True, "image_preview_count": visual_count,
            "direct_visual_input_enabled": model_capability == "vision",
            "limitations": [
                "Model capability is declared; an endpoint capability test is still required for image input.",
                "Text-only models can read descriptions and run scientific tools; image semantics are not inferred.",
                "Previews and summaries are sampled inspection aids and must not replace scientific computation inputs.",
                "Audio descriptions are metadata; PDF text is limited to five pages; video preview is one frame.",
            ],
        },
        "limits": {
            "max_files": max_files, "max_file_bytes": max_file_bytes,
            "max_archive_samples": max_archive_samples, "max_archive_sample_bytes": max_archive_sample_bytes,
            "max_archive_total_bytes": max_archive_total_bytes, "max_text_bytes": MAX_TEXT_BYTES,
        },
        "decoder_requirements": sorted({hint for item in records for hint in item["decoder_hints"]}),
        "files": records,
    }
    (output_root / "DATA_OVERVIEW.json").write_text(
        json.dumps(_json_safe(report), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    (output_root / "DATA_OVERVIEW.md").write_text(_markdown(report), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--model-capability", choices=["text", "vision", "unknown"], default="text")
    parser.add_argument("--max-files", type=int, default=10000)
    parser.add_argument("--max-file-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--max-archive-samples", type=int, default=32)
    parser.add_argument("--max-archive-sample-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--max-archive-total-bytes", type=int, default=1024 * 1024 * 1024)
    parser.add_argument("--strict-multimodal", action="store_true")
    args = parser.parse_args()
    try:
        report = prepare_multimodal_data(
            args.data_dir, args.output_dir or args.data_dir / ".qwen",
            model_capability=args.model_capability, max_files=args.max_files,
            max_file_bytes=args.max_file_bytes, max_archive_samples=args.max_archive_samples,
            max_archive_sample_bytes=args.max_archive_sample_bytes, max_archive_total_bytes=args.max_archive_total_bytes,
        )
    except (ValueError, FileNotFoundError) as error:
        parser.error(str(error))
    print(json.dumps(report["summary"], sort_keys=True))
    if args.strict_multimodal and report["summary"]["unsupported_count"]:
        print("Unsupported inputs: see DATA_OVERVIEW.json for explicit reasons.", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
