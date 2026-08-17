#!/usr/bin/env bash
# patches/apply_patches.sh
# Copy new/modified modules from patches/ into the Zotero2Readwise package checkout
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PATCH_DIR="$ROOT_DIR/patches/zotero2readwise"
TARGET_DIR="$(pwd)/Zotero2Readwise/zotero2readwise"

if [ ! -d "$TARGET_DIR" ]; then
  echo "Target directory $TARGET_DIR does not exist. Make sure the workflow checked out Zotero2Readwise into ./Zotero2Readwise"
  exit 1
fi

echo "Applying patches from $PATCH_DIR -> $TARGET_DIR"

# Backup originals
BACKUP_DIR="$TARGET_DIR/.backup_$(date +%s)"
mkdir -p "$BACKUP_DIR"

for f in "$PATCH_DIR"/*.py; do
  filename=$(basename "$f")
  if [ -f "$TARGET_DIR/$filename" ]; then
    echo "Backing up $TARGET_DIR/$filename -> $BACKUP_DIR/"
    mv "$TARGET_DIR/$filename" "$BACKUP_DIR/"
  fi
  echo "Copying $f -> $TARGET_DIR/"
  cp "$f" "$TARGET_DIR/"
done

echo "Patches applied. Backups stored in $BACKUP_DIR"
