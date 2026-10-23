import json
import logging
import os
import time
import uuid
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from pdf_processor import PDFProcessor, ALL_EXAMBOARDS, ALL_SUBJECTS, filter_bubbles

load_dotenv()

# pdf_processor configures the root logger's handlers (file + stream) at
# import time above, so this logger inherits them via propagation.
logger = logging.getLogger(__name__)

import firebase_admin
from firebase_admin import credentials, storage as fb_storage

_SERVICE_ACCOUNT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serviceAccountKey.json")
_firebase_ready = False
if os.path.exists(_SERVICE_ACCOUNT):
    _cred = credentials.Certificate(_SERVICE_ACCOUNT)
    firebase_admin.initialize_app(_cred, {"storageBucket": os.getenv("FIREBASE_STORAGE_BUCKET", "")})
    _firebase_ready = True

app = Flask(__name__)
CORS(app)

app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
app.config["UPLOAD_FOLDER"] = "uploads"

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs("images", exist_ok=True)

pdf_processor = PDFProcessor()

PAGE_BATCH_SIZE = 10


def _state_path(job_id: str) -> str:
    return os.path.join(app.config["UPLOAD_FOLDER"], f"{job_id}.state.json")


def _finish_batch(job_id: str, filepath: str, result: dict) -> None:
    """Keep the upload and its question-numbering state for the next
    "Extract More" batch, or clean both up once the document is done. The
    state is internal to extraction, so it never goes to the browser."""
    marker_state = result.pop("marker_state", None)
    if result.get("metadata", {}).get("has_more", False):
        with open(_state_path(job_id), "w") as f:
            json.dump(marker_state, f)
        return
    for path in (filepath, _state_path(job_id)):
        if os.path.exists(path):
            os.remove(path)


@app.route("/api/process-pdf", methods=["POST"])
def process_pdf():
    try:
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"error": "No file selected"}), 400

        if not file.filename.lower().endswith(".pdf"):
            return jsonify({"error": "File must be a PDF"}), 400

        subject_id = request.form.get("subject_id") or None
        board_id = request.form.get("board_id") or None
        topic_id = request.form.get("topic_id") or None

        start_time = time.time()

        job_id = str(uuid.uuid4())
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], f"{job_id}.pdf")
        file.save(filepath)

        try:
            result = pdf_processor.process_pdf(
                filepath, subject_id=subject_id, board_id=board_id, topic_id=topic_id,
                start_page=0, page_count=PAGE_BATCH_SIZE
            )
        except Exception:
            if os.path.exists(filepath):
                os.remove(filepath)
            raise

        processing_time = round(time.time() - start_time, 2)
        has_more = result.get("metadata", {}).get("has_more", False)

        result["metadata"] = {
            **result.get("metadata", {}),
            "processing_time": processing_time,
            "filename": filename,
            "subject_id": subject_id,
            "board_id": board_id,
            "topic_id": topic_id,
            "job_id": job_id if has_more else None,
        }
        _finish_batch(job_id, filepath, result)

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": f"Processing failed: {str(e)}"}), 500


ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}


@app.route("/api/process-image", methods=["POST"])
def process_image():
    try:
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"error": "No file selected"}), 400

        ext = os.path.splitext(file.filename.lower())[1]
        if ext not in ALLOWED_IMAGE_EXTENSIONS:
            return jsonify({"error": "File must be an image (png, jpg, jpeg, webp, bmp, tiff)"}), 400

        subject_id = request.form.get("subject_id") or None
        board_id = request.form.get("board_id") or None
        topic_id = request.form.get("topic_id") or None

        start_time = time.time()

        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], f"{uuid.uuid4()}{ext}")
        file.save(filepath)

        try:
            result = pdf_processor.process_image(
                filepath, subject_id=subject_id, board_id=board_id, topic_id=topic_id
            )
        finally:
            if os.path.exists(filepath):
                os.remove(filepath)

        processing_time = round(time.time() - start_time, 2)
        result["metadata"] = {
            **result.get("metadata", {}),
            "processing_time": processing_time,
            "filename": filename,
            "subject_id": subject_id,
            "board_id": board_id,
            "topic_id": topic_id,
        }

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": f"Processing failed: {str(e)}"}), 500


@app.route("/api/process-pdf-batch", methods=["POST"])
def process_pdf_batch():
    try:
        data = request.get_json(silent=True) or {}
        job_id = data.get("job_id")
        start_page = data.get("start_page", 0)
        subject_id = data.get("subject_id") or None
        board_id = data.get("board_id") or None
        topic_id = data.get("topic_id") or None

        if not job_id:
            return jsonify({"error": "Missing job_id"}), 400
        try:
            job_id = str(uuid.UUID(job_id))  # it becomes part of file paths below
        except ValueError:
            return jsonify({"error": "Invalid job_id"}), 400

        filepath = os.path.join(app.config["UPLOAD_FOLDER"], f"{job_id}.pdf")
        if not os.path.exists(filepath):
            return jsonify({"error": "No pending file found for this job_id — it may have already finished or expired"}), 404

        marker_state = None
        if os.path.exists(_state_path(job_id)):
            with open(_state_path(job_id)) as f:
                marker_state = json.load(f)

        start_time = time.time()
        result = pdf_processor.process_pdf(
            filepath, subject_id=subject_id, board_id=board_id, topic_id=topic_id,
            start_page=start_page, page_count=PAGE_BATCH_SIZE, marker_state=marker_state,
        )
        processing_time = round(time.time() - start_time, 2)
        has_more = result.get("metadata", {}).get("has_more", False)

        result["metadata"] = {
            **result.get("metadata", {}),
            "processing_time": processing_time,
            "subject_id": subject_id,
            "board_id": board_id,
            "topic_id": topic_id,
            "job_id": job_id if has_more else None,
        }
        _finish_batch(job_id, filepath, result)

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": f"Processing failed: {str(e)}"}), 500


@app.route("/api/upload-image", methods=["POST"])
def upload_image():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400
    ext = os.path.splitext(secure_filename(file.filename))[1].lower()
    if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
        return jsonify({"error": "Invalid image type"}), 400
    filename = f"{uuid.uuid4()}{ext}"
    file.save(os.path.join("images", filename))
    return jsonify({"path": f"/api/images/{filename}"})


@app.route("/api/save-to-firebase", methods=["POST"])
def save_to_firebase():
    if not _firebase_ready:
        return jsonify({"error": "Firebase not configured (serviceAccountKey.json missing)"}), 503

    data = request.get_json(silent=True) or {}
    questions = data.get("questions", [])

    local_paths = set()
    for q in questions:
        imgs = q.get("image", [])
        if isinstance(imgs, str):
            imgs = [imgs]
        for img in imgs:
            if img and img.startswith("/api/images/"):
                local_paths.add(img)

    content_types = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif",
        ".webp": "image/webp", ".bmp": "image/bmp",
    }

    bucket = fb_storage.bucket()
    url_map = {}
    missing = []
    for local_path in local_paths:
        filename = os.path.basename(local_path)
        file_path = os.path.join("images", filename)
        if not os.path.exists(file_path):
            # The local file is gone — most often because the shared
            # images/ directory has no per-session isolation, so an
            # unrelated cleanup (another save's /api/cleanup-images sweep,
            # or manual housekeeping) can delete a file a different,
            # still-open session is holding a stale reference to. Surface
            # this instead of silently dropping the image, so a user
            # editing a long-lived session can tell why some pictures
            # didn't make it to Firebase rather than getting a save that
            # quietly succeeds short of what they expected.
            logger.warning(f"save-to-firebase: local image missing, skipped: {file_path}")
            missing.append(local_path)
            continue
        ext = os.path.splitext(filename)[1].lower()
        content_type = content_types.get(ext, "image/png")
        blob = bucket.blob(f"images/{filename}")
        blob.upload_from_filename(file_path, content_type=content_type)
        # Mobile app reconstructs the URL itself from this short path
        # (environment.images base + filename + ?alt=media) — no token needed.
        url_map[local_path] = f"/images/{filename}"

    return jsonify({"url_map": url_map, "missing": missing})


@app.route("/api/examboards", methods=["GET"])
def get_examboards():
    return jsonify({"examboards": ALL_EXAMBOARDS})


@app.route("/api/subjects", methods=["GET"])
def get_subjects():
    # subjects.json's own board_id is just a nominal default — the same subject's
    # topics/bubbles can genuinely span multiple boards (e.g. Biology has both AQA
    # and Edexcel content). Derive real availability from the bubbles themselves.
    def norm_id(v):
        return v.get("_id") if isinstance(v, dict) else v

    subject_boards = {}
    for b in filter_bubbles():
        topic = b.get("topic_id") or {}
        sid = norm_id(topic.get("subject_id"))
        bid = norm_id(topic.get("board_id"))
        if sid and bid:
            subject_boards.setdefault(sid, set()).add(bid)

    enriched = [
        {**s, "available_board_ids": sorted(subject_boards.get(s["_id"], []))}
        for s in ALL_SUBJECTS
    ]
    return jsonify({"subjects": enriched})


@app.route("/api/topics", methods=["GET"])
def get_topics():
    subject_id = request.args.get("subject_id") or None
    board_id = request.args.get("board_id") or None

    seen = {}
    for b in filter_bubbles(subject_id, board_id):
        topic = b.get("topic_id") or {}
        tid = topic.get("_id")
        if tid and tid not in seen:
            seen[tid] = {"_id": tid, "name": topic.get("name", "")}

    topics = sorted(seen.values(), key=lambda t: t["name"])
    return jsonify({"topics": topics})


@app.route("/api/bubbles", methods=["GET"])
def get_bubbles():
    subject_id = request.args.get("subject_id") or None
    board_id = request.args.get("board_id") or None
    topic_id = request.args.get("topic_id") or None
    return jsonify({"bubbles": filter_bubbles(subject_id, board_id, topic_id)})


@app.route("/api/cleanup-images", methods=["POST"])
def cleanup_images():
    data = request.get_json(silent=True) or {}
    keep_paths = set(data.get("keep", []))
    keep_filenames = {os.path.basename(p) for p in keep_paths}
    deleted = 0
    images_dir = "images"
    for filename in os.listdir(images_dir):
        if filename not in keep_filenames:
            try:
                os.remove(os.path.join(images_dir, filename))
                deleted += 1
            except OSError:
                pass
    return jsonify({"deleted": deleted})


@app.route("/api/images/<filename>")
def serve_image(filename):
    return send_from_directory("images", filename)


@app.route("/api/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy"})


if __name__ == "__main__":
    app.run(debug=True, port=5001)
