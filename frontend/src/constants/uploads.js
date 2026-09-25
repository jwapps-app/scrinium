// What the server will ingest — mirrors ACCEPTED_SUFFIXES in
// backend/app/services/intake.py. Keep the two lists in step.
export const ACCEPTED_SUFFIXES = [
  '.pdf',
  '.png',
  '.jpg',
  '.jpeg',
  '.tif',
  '.tiff',
  '.bmp',
  '.webp',
  // Text-native formats: no OCR — text is extracted directly at intake.
  '.txt',
  '.md',
  '.epub',
  '.docx',
  '.xlsx',
  '.pptx',
  '.odt',
]

// The file picker's accept attribute. image/* stays so phones offer the
// camera roll, which lists by kind rather than by extension.
export const UPLOAD_ACCEPT = [...ACCEPTED_SUFFIXES, 'image/*'].join(',')
