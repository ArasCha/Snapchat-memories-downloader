
import re
import html
import json
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse
import mimetypes
import sys

import time
import requests
from requests.exceptions import SSLError, HTTPError
from bs4 import BeautifulSoup

from PIL import Image
from PIL.PngImagePlugin import PngInfo
import piexif



ONCLICK_RE = re.compile(r"downloadMemories\(\s*'([^']+)'", re.I)

def parse_iso8601_utc(date_str: str) -> str:
    dt = datetime.strptime(date_str.strip(), "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")  # e.g. 2025-11-12T21:05:03Z

def parse_lat_lon(location_str: str):
    m = re.search(r"Latitude,\s*Longitude:\s*([+-]?\d+(?:\.\d+)?),\s*([+-]?\d+(?:\.\d+)?)", location_str)
    if not m:
        return None, None
    return float(m.group(1)), float(m.group(2))

def safe_filename(s: str) -> str:
    s = re.sub(r"[^\w\-\.]+", "_", s.strip())
    return (s.strip("_") or "file")[:180]

def extract_download_url(row) -> str | None:
    a = row.select_one('a[onclick*="downloadMemories"]')
    if not a or not a.get("onclick"):
        return None
    m = ONCLICK_RE.search(a["onclick"])
    if not m:
        return None
    return html.unescape(m.group(1))  # turns &amp; into &

def guess_ext(url: str, content_type: str | None) -> str:
    # Prefer Content-Type, fallback to URL path, then mimetypes.
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        ext = mimetypes.guess_extension(ct) or ""
        # common fixups
        if ct == "video/mp4":
            return ".mp4"
        if ct == "image/jpeg":
            return ".jpg"
        if ext:
            return ext

    path = urlparse(url).path
    suffix = Path(path).suffix
    if suffix:
        return suffix

    return ""  # unknown

def download_file(url: str, out_stem: Path, kind_norm: str, timeout=60) -> tuple[Path, str | None]:
    """
    Downloads URL to disk. `out_stem` is a path WITHOUT extension.
    Returns (final_path_with_ext, content_type)
    """
    out_stem.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_stem.with_suffix(".download")

    max_retries = 3

    for attempt in range(1, max_retries + 1):
        try:
            with requests.get(url, stream=True, timeout=timeout) as r:
                r.raise_for_status()
                content_type = r.headers.get("Content-Type")
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
            break  # success
        except SSLError:
            if attempt == max_retries:
                raise
            time.sleep(60)

    ext = guess_ext(url, content_type)

    if ext == "" and kind_norm == "image":
        final_path = out_stem.with_suffix(".jpg")
    else:
        final_path = out_stem.with_suffix(ext)

    tmp_path.replace(final_path)
    return final_path, content_type

# -------- Video metadata (ffmpeg) --------

def to_iso6709(lat, lon) -> str:
    if lat is None or lon is None:
        return ""
    return f"{lat:+.4f}{lon:+.4f}/"

def write_video_metadata_ffmpeg(in_path: Path, out_path: Path, date_iso: str, location_text: str, lat, lon):
    iso6709 = to_iso6709(lat, lon)
    cmd = [
        "ffmpeg", "-y",
        "-i", str(in_path),
        "-map", "0", "-c", "copy",
        "-metadata", f"creation_time={date_iso}",
        "-metadata", f"comment=Location: {location_text} | Date: {date_iso}",
    ]
    if iso6709:
        cmd += ["-metadata", f"com.apple.quicktime.location.ISO6709={iso6709}"]
        cmd += ["-metadata", f"location={iso6709}"]

    cmd += [str(out_path)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# -------- Image metadata (EXIF for JPEG/TIFF via piexif; PNG text via Pillow) --------

def _deg_to_dms_rational(deg_float: float):
    deg_abs = abs(deg_float)
    d = int(deg_abs)
    m_float = (deg_abs - d) * 60
    m = int(m_float)
    s = (m_float - m) * 60
    # EXIF expects rationals
    return ((d, 1), (m, 1), (int(round(s * 100)), 100))

def tag_jpeg_tiff_exif(path: Path, date_str_for_exif: str, lat, lon, location_text: str):
    if piexif is None:
        raise RuntimeError("piexif not installed")

    exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    # Basic timestamps
    exif_dict["0th"][piexif.ImageIFD.DateTime] = date_str_for_exif
    exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = date_str_for_exif
    exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = date_str_for_exif

    # Comment-ish field (UserComment is more structured; XPComment is Windows-specific)
    exif_dict["0th"][piexif.ImageIFD.ImageDescription] = f"Location: {location_text}"

    # GPS
    if lat is not None and lon is not None:
        exif_dict["GPS"][piexif.GPSIFD.GPSLatitudeRef] = "N" if lat >= 0 else "S"
        exif_dict["GPS"][piexif.GPSIFD.GPSLongitudeRef] = "E" if lon >= 0 else "W"
        exif_dict["GPS"][piexif.GPSIFD.GPSLatitude] = _deg_to_dms_rational(lat)
        exif_dict["GPS"][piexif.GPSIFD.GPSLongitude] = _deg_to_dms_rational(lon)

    exif_bytes = piexif.dump(exif_dict)
    piexif.insert(exif_bytes, str(path))

def tag_png_text(path: Path, date_iso: str, location_text: str):
    if Image is None or PngInfo is None:
        raise RuntimeError("Pillow not installed")

    with Image.open(path) as im:
        meta = PngInfo()
        meta.add_text("Creation Time", date_iso)
        meta.add_text("Location", location_text)
        meta.add_text("Comment", f"Location: {location_text} | Date: {date_iso}")
        # Re-save (this rewrites the PNG file)
        tmp = path.with_suffix(".tagged.png")
        im.save(tmp, pnginfo=meta)
    tmp.replace(path)

def is_zip(file_path, content_type) -> bool:

    is_zip = False

    # 1) header-based
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct in {"application/zip", "application/x-zip-compressed"}:
            is_zip = True

    # 2) signature-based (robust)
    if not is_zip:
        try:
            with open(file_path, "rb") as f:
                sig = f.read(4)
            if sig == b"PK\x03\x04":  # standard ZIP local file header
                is_zip = True
        except OSError:
            pass
    return is_zip

def save_zip(file_path, suffix):
    # Ensure it is saved as .zip (keep the payload untouched)
    zip_path = file_path if suffix == ".zip" else file_path.with_suffix(".zip")
    if zip_path != file_path:
        file_path.replace(zip_path)
        file_path = zip_path

    print(f"Saved ZIP -> {file_path.name}")

def add_line_to_file(file_path: str | Path, text: str) -> None:
    """
    Append `text` as a new line to `file_path`. Creates the file if it doesn't exist.
    """
    file_path = Path(file_path)
    with file_path.open("a", encoding = "utf-8", newline="") as f:
        f.write(text + "\n")


def get_html_and_out_dir() -> tuple[str, str]:
    """
    Returns html memories history path and output directory path and starting index
    Creates output directory if not created
    """

    BASE_DIR = get_base_dir()

    params_path = BASE_DIR / "params.json"

    with params_path.open("r", encoding="utf-8") as f:
        params = json.load(f)

    html_path = Path(params["memories_history_path"])
    out_dir = Path(params["output_directory"])
    starting_index = params["starting_index"]
    
    out_dir.mkdir(parents=True, exist_ok=True)  # create out_dir if it doesn't exist (including parents)

    return html_path, out_dir, starting_index


def get_base_dir() -> Path:

    # Exécuté via PyInstaller
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # Exécuté en .py normal
    return Path(__file__).resolve().parent







if __name__ == "__main__":

    html_path, out_dir, starting_index = get_html_and_out_dir()

    soup = BeautifulSoup(html_path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
    tbody = soup.select_one("body > div.rightpanel > table > tbody")
    rows = tbody.select("tr") if tbody else []
    print(f"Number of files: {len(rows) - 1}") # on retire la première qui n'est pas un fichier mais le titre des col du tableau

    for i, row in enumerate(rows, start=0):
        tds = row.select("td")
        if len(tds) < 4:
            continue

        if i < starting_index:
            continue
        
        date_str = tds[0].get_text(strip=True)          # "2025-11-12 21:05:03 UTC"
        kind = tds[1].get_text(strip=True)              # "Video" or "Image"
        location_str = tds[2].get_text(strip=True)      # "Latitude, Longitude: ..."
        url = extract_download_url(row)

        kind_norm = kind.strip().lower()
        
        date_iso = parse_iso8601_utc(date_str)
        # EXIF datetime format is "YYYY:MM:DD HH:MM:SS"
        dt_exif = datetime.strptime(date_str.strip(), "%Y-%m-%d %H:%M:%S UTC").strftime("%Y:%m:%d %H:%M:%S")

        lat, lon = parse_lat_lon(location_str)

        base = safe_filename(f"{i}_{date_iso}".replace(":", ""))
        out_stem = out_dir / base

        print(f"[{i}/{len(rows)}] Downloading {kind} {date_str}")
        try:
            file_path, content_type = download_file(url, out_stem, kind_norm)
        except HTTPError:
            not_saved_file_name = "not_saved.txt"
            add_line_to_file(f"{out_dir}/{not_saved_file_name}", f"{i}_{kind} {date_str}")
            print(f"HTTP Error, saved file name in {not_saved_file_name}")
            continue

        suffix = file_path.suffix.lower()

        # Save file with metadata
        try:

            if is_zip(file_path, content_type):
                save_zip(file_path, suffix)

            elif kind_norm == "video" and shutil.which("ffmpeg"):
                tagged = file_path.with_suffix(file_path.suffix.replace(".", ".tagged.", 1))
                write_video_metadata_ffmpeg(file_path, tagged, date_iso, location_str, lat, lon)
                file_path.unlink(missing_ok=True)
                tagged.replace(file_path)
                print(f"   Saved video -> {file_path.name}")


            elif kind_norm == "image":
                if suffix in {".jpg", ".jpeg", ".tif", ".tiff"}:
                    tag_jpeg_tiff_exif(file_path, dt_exif, lat, lon, location_str)
                    print(f"   Saved EXIF -> {file_path.name}")
                elif suffix == ".png":
                    tag_png_text(file_path, date_iso, location_str)
                    print(f"   Saved PNG text -> {file_path.name}")
                else:
                    # Unknown image type (e.g., HEIC/WebP) or missing libs -> sidecar
                    sidecar = file_path.with_suffix(file_path.suffix + ".json")
                    sidecar.write_text(json.dumps(
                        {"kind": kind, "date": date_iso, "location": location_str, "lat": lat, "lon": lon, "content_type": content_type},
                        indent=2
                    ))
                    print(f"   Image type unsupported for tagging; wrote sidecar -> {sidecar.name}")

            else:
                # No ffmpeg for video, etc.
                sidecar = file_path.with_suffix(file_path.suffix + ".json")
                sidecar.write_text(json.dumps(
                    {"kind": kind, "date": date_iso, "location": location_str, "lat": lat, "lon": lon, "content_type": content_type},
                    indent=2
                ))
                print(f"   Tagger not available; wrote sidecar -> {sidecar.name}")

        except Exception as e:
            # Never lose the downloaded file; write sidecar on failure.
            print(str(e))
            sidecar = file_path.with_suffix(file_path.suffix + ".json")
            sidecar.write_text(json.dumps(
                {"kind": kind, "date": date_iso, "location": location_str, "lat": lat, "lon": lon, "content_type": content_type, "tag_error": str(e)},
                indent=2
            ))
            print(f"   Tagging failed; wrote sidecar -> {sidecar.name}")

        input("\nPress Enter to close...")