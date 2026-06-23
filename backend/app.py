import os
import time
import uuid
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from pdf_processor import PDFProcessor, ALL_EXAMBOARDS, ALL_SUBJECTS, filter_bubbles

load_dotenv()

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

        start_time = time.time()

        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(filepath)

        try:
            result = pdf_processor.process_pdf(filepath, subject_id=subject_id, board_id=board_id)
            processing_time = round(time.time() - start_time, 2)

            result["metadata"] = {
                **result.get("metadata", {}),
                "processing_time": processing_time,
                "filename": filename,
                "subject_id": subject_id,
                "board_id": board_id,
            }

            return jsonify(result)

        finally:
            if os.path.exists(filepath):
                os.remove(filepath)

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
    for local_path in local_paths:
        filename = os.path.basename(local_path)
        file_path = os.path.join("images", filename)
        if not os.path.exists(file_path):
            continue
        ext = os.path.splitext(filename)[1].lower()
        content_type = content_types.get(ext, "image/png")
        blob = bucket.blob(f"images/{filename}")
        blob.upload_from_filename(file_path, content_type=content_type)
        # Mobile app reconstructs the URL itself from this short path
        # (environment.images base + filename + ?alt=media) — no token needed.
        url_map[local_path] = f"/images/{filename}"

    return jsonify({"url_map": url_map})


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


@app.route("/api/bubbles", methods=["GET"])
def get_bubbles():
    subject_id = request.args.get("subject_id") or None
    board_id = request.args.get("board_id") or None
    return jsonify({"bubbles": filter_bubbles(subject_id, board_id)})


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
