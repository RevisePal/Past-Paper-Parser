import React, { useCallback } from "react";
import { useDropzone } from "react-dropzone";
import axios from "axios";
import "./FileUpload.css";

const IMAGE_TYPES = new Set([
  "image/png", "image/jpeg", "image/webp", "image/bmp", "image/tiff",
]);

const FileUpload = ({ onFileProcessed, onError, onLoadingChange, subjectId, boardId, topicId, disabled }) => {
  const onDrop = useCallback(
    async (acceptedFiles) => {
      if (disabled) {
        onError("Please select an exam board and subject first");
        return;
      }

      const file = acceptedFiles[0];

      if (!file) return;

      const isPdf = file.type === "application/pdf";
      const isImage = IMAGE_TYPES.has(file.type);
      if (!isPdf && !isImage) {
        onError("Please upload a PDF or an image (png, jpg, webp, bmp, tiff)");
        return;
      }

      onLoadingChange(true);

      const formData = new FormData();
      formData.append("file", file);
      formData.append("subject_id", subjectId || "");
      formData.append("board_id", boardId || "");
      formData.append("topic_id", topicId || "");

      try {
        // A single-image upload (e.g. a screenshot of one question) has no
        // multi-page structure to segment — /api/process-image OCRs the
        // whole image and treats it as exactly one question, with no
        // pagination/"Extract More" step, unlike the PDF path.
        const response = await axios.post(
          `http://127.0.0.1:5001/api/${isPdf ? "process-pdf" : "process-image"}`,
          formData,
          {
            headers: {
              "Content-Type": "multipart/form-data",
            },
            timeout: 300000,
          }
        );

        onFileProcessed(response.data);
      } catch (error) {
        console.error("Error processing file:", error);
        onError(
          error.response?.data?.error ||
            `Failed to process ${isPdf ? "PDF" : "image"}. Please try again.`
        );
      } finally {
        onLoadingChange(false);
      }
    },
    [onFileProcessed, onError, onLoadingChange, subjectId, boardId, topicId, disabled]
  );

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: {
      "application/pdf": [".pdf"],
      "image/png": [".png"],
      "image/jpeg": [".jpg", ".jpeg"],
      "image/webp": [".webp"],
      "image/bmp": [".bmp"],
      "image/tiff": [".tiff", ".tif"],
    },
    multiple: false,
    disabled,
  });

  return (
    <div className="file-upload-container">
      <div
        {...getRootProps()}
        className={`dropzone ${isDragActive ? "active" : ""} ${disabled ? "disabled" : ""}`}
      >
        <input {...getInputProps()} />
        <div className="upload-content">
          <div className="upload-icon">📄</div>
          {disabled ? (
            <p>Select an exam board and subject above to enable upload</p>
          ) : isDragActive ? (
            <p>Drop the file here...</p>
          ) : (
            <>
              <h3>Upload Past Paper PDF or Question Screenshot</h3>
              <p>Drag and drop a PDF or image file here, or click to select</p>
              <button className="upload-button">Choose File</button>
            </>
          )}
        </div>
      </div>
    </div>
  );
};

export default FileUpload;
